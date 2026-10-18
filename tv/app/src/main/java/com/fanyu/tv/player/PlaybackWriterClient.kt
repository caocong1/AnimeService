package com.fanyu.tv.player

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.net.URLEncoder
import java.security.SecureRandom

/**
 * Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md; owner client contract round 17,
 * review round 19), pure Kotlin so the JVM tests cover it. The player uses it only after the bootstrap
 * advertised the capability.
 * - identity: [mediaKey] is the canonical key the server gave for the loaded media; every validated reply
 *   must echo requested_media_key and keep media_key bound to it;
 * - every job captures epoch, lane, generation, token and an immutable snapshot when it is created and is
 *   dropped, without consuming a sequence, if the fence or lane moved before it runs; sequences are
 *   assigned at execution, in order, under a short lock that is never held across the network;
 * - a save succeeds only with a 2xx, ok=true, no ignored flag and the packet's own generation/sequence;
 * - a transport exception is a network failure bound to its epoch; cancellation is rethrown; disabled,
 *   404, 400, 401, 403 or a bad body halt writing for this media (position kept, error visible, no legacy
 *   fallback, no automatic claim); a server or network fault keeps the fence with the position unsaved;
 * - fenced/expired/session_required stop writing until an explicit reclaim;
 * - disposed is terminal for everything except one final snapshot captured before disposal, whose
 *   answer updates nobody;
 * - a claim answered after a newer claim, a replayed claim or a retried claim never counts as live
 *   ownership without a state check in the same epoch.
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

    /** HTTP status (0 when the server could not be reached, SERVER_CHANGED when the bound server moved) and the JSON object body. */
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
    private class Ticket(val epoch: Long, val lane: Long, val generation: Long)
    private class Captured(val ticket: Ticket, val token: String, val path: String, val extra: Map<String, Any?>, val keepalive: Boolean, val permit: Boolean)
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
    val generation: Long get() = fence?.generation ?: conflict?.generation ?: observedGeneration

    private var fence: Fence? = null
    private var epoch = 0L
    private var lane = 0L
    private var sequence = 0L
    private var observedGeneration = 0L
    private var claiming = false
    private var finalTicket: Ticket? = null
    private val order = Mutex()        // regular saves run one at a time, in call order
    private val identity = Mutex()     // sequence assignment and fence checks; never held across the network
    private val listeners = mutableListOf<(Status) -> Unit>()

    fun onChange(listener: (Status) -> Unit) { listeners += listener }

    private fun notify() { listeners.toList().forEach { it(status) } }
    private fun set(next: Status, nextConflict: Conflict?) { status = next; conflict = nextConflict; notify() }
    private fun live(t: Ticket) = t.epoch == epoch && status != Status.DISPOSED

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

    private fun bound(body: Map<String, Any?>): Boolean {
        val echo = body["requested_media_key"]
        return echo == null || echo == mediaKey
    }

    /** A short network/server fault keeps the fence; a protocol or authorization fault halts this media. */
    private fun fail(t: Ticket, kind: String, http: Int = 0): SendResult {
        if (!live(t)) return SendResult(false, "stale_response")
        lastError = if (http > 0) "${kind}_$http" else kind
        unsaved = true
        if (kind in HALT) { fence = null; set(Status.HALTED, conflict) } else set(if (fence != null) Status.WRITING else Status.ERROR, conflict)
        return SendResult(false, lastError)
    }

    private suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response =
        try { transport.post(path, body, keepalive) } catch (e: CancellationException) { throw e } catch (e: Exception) { Response(0, null) }

    private suspend fun get(path: String): Response =
        try { transport.get(path) } catch (e: CancellationException) { throw e } catch (e: Exception) { Response(0, null) }

    /** Reads who holds progress without claiming anything; remembers the generation for the next open. */
    suspend fun state(): StateResult {
        if (status == Status.DISPOSED) return StateResult(false, "disposed", generation, false, null)
        val t = Ticket(epoch, lane, fence?.generation ?: 0)
        val c = classify(get("$STATE?media_key=" + URLEncoder.encode(mediaKey, "UTF-8")))
        if (!live(t)) return StateResult(false, "stale_response", generation, false, null)
        if (c.kind == "disabled" || c.kind == "protocol" || c.kind == "unauthorized") {
            fail(t, c.kind, c.http); if (fence == null) set(Status.UNSUPPORTED, null)
            return StateResult(false, lastError, generation, false, null)
        }
        if (c.kind != "ok") return StateResult(false, c.kind, generation, false, null)
        val body = c.body!!
        val gen = safeLong(body["generation"])
        if (body["media_key"] != mediaKey || !bound(body) || gen == null || body["active"] !is Boolean) {
            fail(t, "protocol", c.http); return StateResult(false, "invalid_state", generation, false, null)
        }
        val active = body["active"] == true
        observedGeneration = gen
        notify()
        return StateResult(true, null, gen, active, if (active) sanitizeLabel(body["device_id"] as? String) else null)
    }

    /** OPEN on an explicit Play/Resume; TAKEOVER only from a visible user action with the generation shown. */
    suspend fun claim(mode: Mode = Mode.OPEN, expectedGeneration: Long? = null): ClaimResult {
        require(mode != Mode.TAKEOVER || expectedGeneration != null) { "takeover needs the generation the user saw" }
        if (status == Status.DISPOSED) return ClaimResult(false, "disposed", generation, null)
        if (claiming) return ClaimResult(false, "claim_in_progress", generation, null)
        claiming = true
        try {
            val expected = expectedGeneration ?: generation
            val requestId = random(16)
            val token = random(32)
            require(requestId.length >= 16 && token.length in 32..128) { "random identifiers are too short" }
            val body = mapOf("media_key" to mediaKey, "session" to session, "request_id" to requestId, "writer_token" to token,
                             "device_id" to device, "expected_generation" to expected, "mode" to mode.wire)
            val t = Ticket(++epoch, ++lane, 0)             // every older claim, job and answer is now inert
            fence = null; sequence = 0; lastError = null
            var attempt = 0
            var r: Response
            do {                                           // a network failure retries the SAME packet, never a second unknown claim
                attempt++
                r = post(CLAIM, body, false)
            } while (r.status == 0 && attempt <= retries && live(t))
            if (!live(t)) return ClaimResult(false, "superseded", generation, null)
            val c = classify(r)
            val b = c.body
            if (c.kind == "conflict" || (c.kind == "ok" && b?.get("ok") == false)) {
                if (b == null || !bound(b)) { fail(t, "protocol", c.http); return ClaimResult(false, lastError, generation, null) }
                val active = b["active"] == true
                val now = Conflict(b["reason"] as? String ?: "conflict", safeLong(b["generation"]) ?: 0L, if (active) sanitizeLabel(b["device_id"] as? String) else null)
                observedGeneration = now.generation
                set(Status.CONFLICT, now)
                return ClaimResult(false, now.reason, now.generation, now.device)
            }
            if (c.kind != "ok" || b == null) { fail(t, c.kind, c.http); return ClaimResult(false, lastError, generation, null) }
            val gen = safeLong(b["generation"])
            val expires = (b["expires"] as? Number)?.toDouble()
            if (b["ok"] != true || b["media_key"] != mediaKey || !bound(b) || gen == null || gen < 1 || expires == null || !expires.isFinite()) {
                fail(t, "protocol", c.http); return ClaimResult(false, lastError, generation, null)
            }
            if (attempt > 1 || b["replayed"] == true) {    // history is not live ownership: confirm through state in this epoch
                val current = state()
                if (!live(t) || !current.supported || !current.active || current.generation != gen) {
                    if (live(t)) { fence = null; set(Status.STALE, Conflict("stale", current.generation, current.device)) }
                    return ClaimResult(false, "verify_failed", current.generation, current.device)
                }
            }
            fence = Fence(gen, token, expires)
            observedGeneration = gen
            unsaved = false
            set(Status.WRITING, null)
            return ClaimResult(true, null, gen, null)
        } finally { claiming = false }
    }

    private fun capture(path: String, extra: Map<String, Any?>, keepalive: Boolean, permit: Boolean): Captured? {
        val f = fence
        if (status != Status.WRITING || f == null) return null
        return Captured(Ticket(epoch, lane, f.generation), f.token, path, extra.toMap(), keepalive, permit)
    }

    private fun skipReason() = if (status == Status.IDLE) "no_claim" else status.name.lowercase()

    private suspend fun execute(c: Captured): SendResult {
        val packet = identity.withLock {
            val t = c.ticket
            val allowed = if (c.permit && finalTicket === t) status != Status.IDLE else live(t) && status == Status.WRITING
            val f = fence
            if (!allowed || f == null || f.generation != t.generation || (!c.permit && t.lane != lane)) return SendResult(false, "stale_response")
            if (sequence >= MAX_SEQUENCE) return SendResult(false, "sequence_exhausted")
            mapOf("media_key" to mediaKey, "session" to session, "generation" to t.generation, "writer_token" to c.token, "sequence" to ++sequence) + c.extra
        }
        val sent = packet["sequence"] as Long
        val cl = classify(post(c.path, packet, c.keepalive))
        val b = cl.body
        val t = c.ticket
        if (c.permit) return if (cl.kind == "ok" && b?.get("ok") == true && safeLong(b["sequence"]) == sent) SendResult(true, null, sequence = sent) else SendResult(false, cl.kind)
        val f = fence
        if (!live(t) || f == null || f.generation != t.generation) return SendResult(false, "stale_response")
        if (cl.kind == "disabled") return fail(t, "disabled", cl.http)
        if (cl.kind == "conflict" || (cl.kind == "ok" && (b?.get("ignored") == true || b?.get("ok") == false))) {
            val reason = b?.get("reason") as? String ?: "rejected"
            if (reason == "fenced" || reason == "expired" || reason == "session_required") {
                fence = null; unsaved = true                 // another writer owns progress now (or ours lapsed): keep the local position
                val active = b?.get("active") == true
                set(if (reason == "expired") Status.EXPIRED else Status.FENCED,
                    Conflict(reason, safeLong(b?.get("generation")) ?: t.generation, if (active) sanitizeLabel(b?.get("device_id") as? String) else null))
                return SendResult(false, reason)
            }
            if (b?.get("ignored") == true) { unsaved = true; notify(); return SendResult(false, reason, ignored = true) }   // out_of_order: not applied
            return fail(t, reason)
        }
        if (cl.kind != "ok" || b == null) return fail(t, cl.kind, cl.http)
        if (b["ok"] != true || safeLong(b["generation"]) != t.generation || safeLong(b["sequence"]) != sent) return fail(t, "protocol", cl.http)
        unsaved = false; lastError = null; notify()
        return SendResult(true, null, sequence = sent)
    }

    private suspend fun send(path: String, extra: Map<String, Any?>): SendResult {
        val c = capture(path, extra, false, false) ?: return SendResult(false, skipReason())
        return order.withLock { execute(c) }
    }

    suspend fun progress(position: Double, duration: Double, playing: Boolean): SendResult =
        send(PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing))

    suspend fun heartbeat(): SendResult = send(HEARTBEAT, emptyMap())

    suspend fun release(): SendResult {
        val r = send(RELEASE, emptyMap())
        if (r.ok) { fence = null; set(Status.RELEASED, null) }
        return r
    }

    /**
     * Captures the last snapshot NOW (the caller passes values it read on its own thread) and returns a job
     * to send later, even after [dispose]: pending regular jobs are dropped so no older position can follow,
     * nothing else is written until an explicit claim, and the answer updates no listener.
     */
    fun final(position: Double, duration: Double, playing: Boolean): suspend () -> SendResult {
        val c = capture(PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing), true, true)
        lane++
        if (c == null) return { SendResult(false, skipReason()) }
        finalTicket = c.ticket
        set(Status.SUSPENDED, null)
        return { execute(c) }
    }

    /** Terminal: no state, claim or regular packet leaves afterwards; a final snapshot captured before still may. */
    fun dispose() {
        epoch++; lane++
        if (finalTicket == null) fence = null
        listeners.clear()
        status = Status.DISPOSED
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

        private fun safeLong(value: Any?): Long? = when (value) {
            is Int -> value.toLong()
            is Long -> value
            is Number -> value.toDouble().let { if (it >= 0 && it % 1.0 == 0.0 && it <= 9007199254740991.0) it.toLong() else null }
            else -> null
        }?.takeIf { it >= 0 }

        fun randomHex(bytes: Int): String {
            val buffer = ByteArray(bytes)
            SecureRandom().nextBytes(buffer)
            return buffer.joinToString("") { "%02x".format(it) }
        }
    }
}
