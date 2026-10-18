package com.fanyu.tv.player

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.net.URLEncoder
import java.security.SecureRandom

/**
 * Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md; owner client contract round 17,
 * reviews rounds 19 and 20), pure Kotlin so the JVM tests cover it. The player uses it only after the
 * bootstrap advertised the capability.
 *
 * All identity state (fence, epoch, lane, sequence, status, the final permit) is read and changed under
 * one short monitor that is never held across the network; regular saves are additionally serialized.
 * - every job captures epoch, lane, generation, token and an immutable snapshot when it is created; at
 *   execution and again when its answer arrives it must still match the current fence, lane and WRITING
 *   status, otherwise it is inert (no sequence consumed, no state touched);
 * - a save succeeds only with a 2xx, ok=true, ignored!=true and its own generation and sequence; state
 *   and claim replies must echo requested_media_key exactly and keep media_key canonical;
 * - disabled/404/400/401/403/bad bodies halt the media: no legacy fallback, no further packets and no new
 *   claim from this instance; a server/network fault keeps the fence with the position unsaved;
 * - final() hands out ONE permit for the last snapshot: pending jobs are dropped, nothing else is written,
 *   the permit is consumed atomically, survives dispose, and its answer updates nobody;
 * - a claim answered after a newer claim, a replayed claim or a retried claim counts only after a state
 *   check in the same epoch; HALTED/UNSUPPORTED instances refuse claims (a new instance re-reads the
 *   capability).
 */
class PlaybackWriterClient(
    private val mediaKey: String,
    private val session: String,
    device: String,
    private val transport: Transport,
    private val random: (Int) -> String = ::randomHex,
    private val retries: Int = 2,
) {
    interface Transport {
        suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response
        suspend fun get(path: String): Response
    }

    /** HTTP status (0 unreachable, SERVER_CHANGED when the bound server moved) and the JSON object body. */
    class Response(val status: Int, val body: Map<String, Any?>?) {
        val ok: Boolean get() = status in 200..299
    }

    enum class Status { IDLE, WRITING, CONFLICT, FENCED, EXPIRED, STALE, RELEASED, SUSPENDED, HALTED, UNSUPPORTED, ERROR, DISPOSED }
    enum class Mode(val wire: String) { OPEN("open"), TAKEOVER("takeover") }

    data class Conflict(val reason: String, val generation: Long, val device: String?)
    data class ClaimResult(val ok: Boolean, val reason: String?, val generation: Long, val device: String?)
    data class SendResult(val ok: Boolean, val reason: String?, val ignored: Boolean = false, val sequence: Long = 0)
    data class StateResult(val supported: Boolean, val reason: String?, val generation: Long, val active: Boolean, val device: String?)

    private class Fence(val generation: Long, val token: String, val expires: Double)
    private class Ticket(val epoch: Long, val lane: Long, val generation: Long, val token: String)
    private class Captured(val ticket: Ticket, val path: String, val extra: Map<String, Any?>, val keepalive: Boolean)
    private class Classified(val kind: String, val body: Map<String, Any?>?, val http: Int)

    val device: String = sanitizeLabel(device) ?: throw IllegalArgumentException("device label required")
    init {
        require(mediaKey.isNotEmpty()) { "mediaKey required" }
        require(session.length >= 16) { "session must have at least 16 characters" }
    }

    @Volatile var status: Status = Status.IDLE
        private set
    @Volatile var conflict: Conflict? = null
        private set
    @Volatile var unsaved: Boolean = false
        private set
    @Volatile var lastError: String? = null
        private set
    val generation: Long get() = synchronized(lock) { fence?.generation ?: conflict?.generation ?: observedGeneration }

    private val lock = Any()
    private val order = Mutex()
    private var fence: Fence? = null
    private var epoch = 0L
    private var lane = 0L
    private var sequence = 0L
    private var observedGeneration = 0L
    private var claiming = false
    private var finalPermit: Captured? = null
    private var finalUsed = false
    private var dirty = false
    private val listeners = mutableListOf<(Status) -> Unit>()

    fun onChange(listener: (Status) -> Unit) { synchronized(lock) { listeners += listener } }

    /** Runs [block] under the identity monitor, then delivers one listener notification if the state changed. */
    private fun <T> update(block: () -> T): T {
        val result: T
        val targets: List<(Status) -> Unit>
        val now: Status
        synchronized(lock) { result = block(); targets = if (dirty) listeners.toList() else emptyList(); dirty = false; now = status }
        targets.forEach { it(now) }
        return result
    }

    private fun transition(next: Status, nextConflict: Conflict?) { status = next; conflict = nextConflict; dirty = true }

    /** The job's identity still matches the live fence, lane and WRITING status (monitor held by the caller). */
    private fun validRegular(t: Ticket): Boolean {
        val f = fence
        return status == Status.WRITING && t.epoch == epoch && t.lane == lane && f != null && f.generation == t.generation && f.token == t.token
    }

    /** Only a 2xx with a JSON object body can succeed; everything else is classified, never trusted. */
    private fun classify(r: Response): Classified {
        val body = r.body
        return when {
            r.status == 0 -> Classified("network", body, 0)
            r.status == SERVER_CHANGED -> Classified("protocol", body, 0)
            r.status == 503 || body?.get("reason") == "disabled" -> Classified("disabled", body, r.status)
            r.status == 409 && body?.get("reason") is String -> Classified("conflict", body, 409)
            r.status == 401 || r.status == 403 -> Classified("unauthorized", body, r.status)
            r.ok && body != null -> Classified("ok", body, r.status)
            r.status >= 500 -> Classified("server", body, r.status)
            else -> Classified("protocol", body, r.status)
        }
    }

    private fun echoed(body: Map<String, Any?>) = body["requested_media_key"] == mediaKey && body["media_key"] == mediaKey

    /** Monitor held: a short network/server fault keeps the fence; a protocol or authorization fault halts the media. */
    private fun failLocked(kind: String, http: Int): SendResult {
        lastError = if (http > 0) "${kind}_$http" else kind
        unsaved = true
        if (kind in HALT) { fence = null; finalPermit = null; transition(Status.HALTED, conflict) }
        else if (status != Status.WRITING) transition(if (status == Status.IDLE) Status.ERROR else status, conflict)
        else dirty = true
        return SendResult(false, lastError)
    }

    private suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response =
        try { transport.post(path, body, keepalive) } catch (e: CancellationException) { throw e } catch (e: Exception) { Response(0, null) }

    private suspend fun get(path: String): Response =
        try { transport.get(path) } catch (e: CancellationException) { throw e } catch (e: Exception) { Response(0, null) }

    /** Reads who holds progress without claiming anything; remembers the generation for the next open. */
    suspend fun state(): StateResult {
        val t = update { if (status == Status.DISPOSED) null else Ticket(epoch, lane, fence?.generation ?: 0, fence?.token ?: "") }
            ?: return StateResult(false, "disposed", generation, false, null)
        val c = classify(get("$STATE?media_key=" + URLEncoder.encode(mediaKey, "UTF-8")))
        return update {
            if (t.epoch != epoch || t.lane != lane || status == Status.DISPOSED) return@update StateResult(false, "stale_response", generation, false, null)
            if (c.kind == "disabled" || c.kind == "protocol" || c.kind == "unauthorized") {
                val untouched = status == Status.IDLE && !claiming    // never claimed: the capability is simply absent here
                failLocked(c.kind, c.http)
                if (untouched) transition(Status.UNSUPPORTED, null)   // either way this instance refuses claims
                return@update StateResult(false, lastError, generation, false, null)
            }
            if (c.kind != "ok") return@update StateResult(false, c.kind, generation, false, null)
            val body = c.body!!
            val gen = safeLong(body["generation"])
            if (!echoed(body) || gen == null || body["active"] !is Boolean) { failLocked("protocol", c.http); return@update StateResult(false, "invalid_state", generation, false, null) }
            val active = body["active"] == true
            observedGeneration = gen
            dirty = true
            StateResult(true, null, gen, active, if (active) sanitizeLabel(body["device_id"] as? String) else null)
        }
    }

    /** OPEN on an explicit Play/Resume; TAKEOVER only from a visible user action with the generation shown. */
    suspend fun claim(mode: Mode = Mode.OPEN, expectedGeneration: Long? = null): ClaimResult {
        require(mode != Mode.TAKEOVER || expectedGeneration != null) { "takeover needs the generation the user saw" }
        val requestId = random(16)
        val token = random(32)
        require(requestId.length >= 16 && token.length in 32..128) { "random identifiers are too short" }
        val started = update {
            if (status == Status.DISPOSED || status == Status.HALTED || status == Status.UNSUPPORTED) return@update ClaimResult(false, status.name.lowercase(), generation, null)
            if (claiming) return@update ClaimResult(false, "claim_in_progress", generation, null)
            claiming = true
            epoch++; lane++; fence = null; sequence = 0; lastError = null; finalPermit = null   // every older claim, job, permit and answer is inert
            null
        }
        if (started != null) return started
        val myEpoch = synchronized(lock) { epoch }
        try {
            val expected = expectedGeneration ?: generation
            val body = mapOf("media_key" to mediaKey, "session" to session, "request_id" to requestId, "writer_token" to token,
                             "device_id" to device, "expected_generation" to expected, "mode" to mode.wire)
            var attempt = 0
            var r: Response
            do {                                           // a network failure retries the SAME packet, never a second unknown claim
                attempt++
                r = post(CLAIM, body, false)
            } while (r.status == 0 && attempt <= retries && synchronized(lock) { epoch == myEpoch && status != Status.DISPOSED })
            val c = classify(r)
            val b = c.body
            val verify = update {
                if (epoch != myEpoch || status == Status.DISPOSED) return@update ClaimResult(false, "superseded", generation, null)
                if (c.kind == "conflict" || (c.kind == "ok" && b?.get("ok") == false)) {
                    val gen = safeLong(b?.get("generation"))
                    if (b == null || !echoed(b) || gen == null || b["active"] !is Boolean) { failLocked("protocol", c.http); return@update ClaimResult(false, lastError, generation, null) }
                    val active = b["active"] == true
                    val now = Conflict(b["reason"] as? String ?: "conflict", gen, if (active) sanitizeLabel(b["device_id"] as? String) else null)
                    observedGeneration = gen
                    transition(Status.CONFLICT, now)
                    return@update ClaimResult(false, now.reason, now.generation, now.device)
                }
                if (c.kind != "ok" || b == null) { failLocked(c.kind, c.http); return@update ClaimResult(false, lastError, generation, null) }
                val gen = safeLong(b["generation"])
                val expires = (b["expires"] as? Number)?.toDouble()
                if (b["ok"] != true || !echoed(b) || gen == null || gen < 1 || expires == null || !expires.isFinite()) { failLocked("protocol", c.http); return@update ClaimResult(false, lastError, generation, null) }
                if (attempt > 1 || b["replayed"] == true) null else { install(gen, token, expires); ClaimResult(true, null, gen, null) }
            }
            if (verify != null) return verify
            val gen = safeLong(b!!["generation"])!!
            val expires = (b["expires"] as Number).toDouble()
            val current = state()                          // history is not live ownership: confirm in this epoch
            return update {
                if (epoch != myEpoch || status == Status.DISPOSED) return@update ClaimResult(false, "superseded", generation, null)
                if (status == Status.HALTED || status == Status.UNSUPPORTED) return@update ClaimResult(false, status.name.lowercase(), generation, null)
                if (!current.supported || !current.active || current.generation != gen) {
                    fence = null; transition(Status.STALE, Conflict("stale", current.generation, current.device))
                    return@update ClaimResult(false, "verify_failed", current.generation, current.device)
                }
                install(gen, token, expires); ClaimResult(true, null, gen, null)
            }
        } finally { synchronized(lock) { claiming = false } }
    }

    private fun install(gen: Long, token: String, expires: Double) {
        fence = Fence(gen, token, expires); observedGeneration = gen; unsaved = false; lastError = null
        transition(Status.WRITING, null)
    }

    private fun skipReason() = if (status == Status.IDLE) "no_claim" else status.name.lowercase()

    private fun strictSave(cl: Classified, t: Ticket, sent: Long): Boolean {
        val b = cl.body
        return cl.kind == "ok" && b != null && b["ok"] == true && b["ignored"] != true && safeLong(b["generation"]) == t.generation && safeLong(b["sequence"]) == sent
    }

    private suspend fun execute(c: Captured): SendResult {
        val t = c.ticket
        val sent = update {
            if (!validRegular(t)) return@update null
            if (sequence >= MAX_SEQUENCE) return@update -1L
            ++sequence
        } ?: return SendResult(false, "stale_response")
        if (sent == -1L) return SendResult(false, "sequence_exhausted")
        val packet = mapOf("media_key" to mediaKey, "session" to session, "generation" to t.generation, "writer_token" to t.token, "sequence" to sent) + c.extra
        val cl = classify(post(c.path, packet, c.keepalive))
        val b = cl.body
        return update {
            if (!validRegular(t)) return@update SendResult(false, "stale_response")   // final, a new claim or a halt happened meanwhile: inert
            if (cl.kind == "disabled") return@update failLocked("disabled", cl.http)
            if (cl.kind == "conflict" || (cl.kind == "ok" && (b?.get("ignored") == true || b?.get("ok") == false))) {
                val reason = b?.get("reason") as? String ?: "rejected"
                if (reason == "fenced" || reason == "expired" || reason == "session_required") {
                    fence = null; unsaved = true                 // another writer owns progress now (or ours lapsed): keep the local position
                    val active = b?.get("active") == true
                    transition(if (reason == "expired") Status.EXPIRED else Status.FENCED,
                        Conflict(reason, safeLong(b?.get("generation")) ?: t.generation, if (active) sanitizeLabel(b?.get("device_id") as? String) else null))
                    return@update SendResult(false, reason)
                }
                if (b?.get("ignored") == true) { unsaved = true; dirty = true; return@update SendResult(false, reason, ignored = true) }   // out_of_order: not applied
                return@update failLocked(reason, 0)
            }
            if (!strictSave(cl, t, sent)) return@update failLocked(if (cl.kind == "ok") "protocol" else cl.kind, cl.http)
            unsaved = false; lastError = null; dirty = true
            SendResult(true, null, sequence = sent)
        }
    }

    private suspend fun send(path: String, extra: Map<String, Any?>): SendResult {
        val c = update { val f = fence; if (status != Status.WRITING || f == null) null else Captured(Ticket(epoch, lane, f.generation, f.token), path, extra.toMap(), false) }
            ?: return SendResult(false, skipReason())
        return order.withLock { execute(c) }
    }

    suspend fun progress(position: Double, duration: Double, playing: Boolean): SendResult =
        send(PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing))

    suspend fun heartbeat(): SendResult = send(HEARTBEAT, emptyMap())

    suspend fun release(): SendResult {
        val r = send(RELEASE, emptyMap())
        if (r.ok) update { fence = null; finalPermit = null; transition(Status.RELEASED, null) }
        return r
    }

    /**
     * Captures the last snapshot NOW (values read on the caller's thread) and returns a job that may run
     * once, even after [dispose]: pending regular jobs are dropped, nothing else is written until an
     * explicit claim, the permit is consumed atomically, and its answer updates no state or listener.
     */
    fun final(position: Double, duration: Double, playing: Boolean): suspend () -> SendResult {
        val c = update {
            val f = fence
            lane++
            if (status != Status.WRITING || f == null) { finalPermit = null; return@update null }
            val captured = Captured(Ticket(epoch, lane, f.generation, f.token), PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing), true)
            finalPermit = captured; finalUsed = false
            fence = null
            transition(Status.SUSPENDED, null)
            captured
        } ?: return { SendResult(false, skipReason()) }
        return {
            val sent = synchronized(lock) {
                if (finalPermit !== c || finalUsed) null else { finalUsed = true; ++sequence }
            }
            if (sent == null) SendResult(false, "stale_response")
            else {
                val packet = mapOf("media_key" to mediaKey, "session" to session, "generation" to c.ticket.generation, "writer_token" to c.ticket.token, "sequence" to sent) + c.extra
                val cl = classify(post(c.path, packet, true))
                if (strictSave(cl, c.ticket, sent)) SendResult(true, null, sequence = sent) else SendResult(false, if (cl.kind == "ok") "protocol" else cl.kind)
            }
        }
    }

    /** Terminal: no state, claim or regular packet leaves afterwards; one final permit captured before still may. */
    fun dispose() {
        synchronized(lock) { epoch++; lane++; fence = null; listeners.clear(); status = Status.DISPOSED; dirty = false }
    }

    companion object {
        const val STATE = "/api/playback-sessions/state"
        const val CLAIM = "/api/playback-sessions/claim"
        const val PROGRESS = "/api/playback-sessions/progress"
        const val HEARTBEAT = "/api/playback-sessions/heartbeat"
        const val RELEASE = "/api/playback-sessions/release"
        const val LABEL_MAX = 32
        /** Transport status meaning the bound server changed underneath: a protocol failure, never a retry target. */
        const val SERVER_CHANGED = -2
        private const val MAX_SEQUENCE = 2147483647L
        private const val MAX_SAFE = 9007199254740991L
        private val HALT = setOf("disabled", "protocol", "unauthorized")

        /** The bootstrap advertises the capability; health or a state 503 never do. */
        fun negotiated(capabilities: Map<String, Any?>?): Boolean {
            val w = capabilities?.get("playback_writer") as? Map<*, *> ?: return false
            return w["protocol"] == "playback-writer/v2" && w["enabled"] == true
        }

        /** A legacy progress reply that carries a durable fence: writing must stop for this media. */
        fun legacyFenced(status: Int, body: Map<String, Any?>?): Boolean =
            status == 409 && (body?.get("reason") == "session_required" || body?.get("reason") == "fenced")

        /** Device labels are display text only: control/format/private-use characters out, trimmed, 32 code points. */
        fun sanitizeLabel(value: String?, max: Int = LABEL_MAX): String? {
            if (value == null) return null
            val kept = StringBuilder()
            var i = 0
            while (i < value.length) {
                val cp = value.codePointAt(i)
                i += Character.charCount(cp)
                val type = Character.getType(cp)
                val unsafe = type == Character.CONTROL.toInt() || type == Character.FORMAT.toInt() ||
                    type == Character.SURROGATE.toInt() || type == Character.PRIVATE_USE.toInt() || type == Character.UNASSIGNED.toInt()
                if (!unsafe) kept.appendCodePoint(cp)
            }
            val trimmed = kept.toString().trim()
            if (trimmed.isEmpty()) return null
            val limit = trimmed.offsetByCodePoints(0, minOf(max, trimmed.codePointCount(0, trimmed.length)))
            return trimmed.substring(0, limit)
        }

        fun deviceLabel(prefix: String, random: (Int) -> String = ::randomHex): String =
            sanitizeLabel("$prefix ${random(2).uppercase()}") ?: prefix

        /** Non-negative integers up to 2^53-1 whatever the JSON parser produced; anything else is invalid. */
        fun safeLong(value: Any?): Long? = when (value) {
            is Int -> value.toLong()
            is Long -> value
            is Number -> value.toDouble().let { if (it.isFinite() && it % 1.0 == 0.0) it.toLong() else null }
            else -> null
        }?.takeIf { it in 0..MAX_SAFE }

        fun randomHex(bytes: Int): String {
            val buffer = ByteArray(bytes)
            SecureRandom().nextBytes(buffer)
            return buffer.joinToString("") { "%02x".format(it) }
        }
    }
}
