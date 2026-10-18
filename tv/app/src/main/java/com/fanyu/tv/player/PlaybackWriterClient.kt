package com.fanyu.tv.player

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.net.URLEncoder
import java.security.SecureRandom

/**
 * Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md, owner client contract round 17),
 * pure Kotlin so the JVM unit tests cover it. The player uses it only after the bootstrap advertised
 * the capability. Guarantees: only an explicit Play/Resume claims; a live writer elsewhere is a visible
 * conflict and a takeover needs a user action, a NEW request id and token, and the generation the
 * user saw; every response is bound to the epoch/media/session/generation it was sent under, so late
 * answers of an old claim, heartbeat or release never touch the current fence; only a 2xx with a valid
 * body succeeds and 409 carries structured reasons; progress/heartbeat/release share one serialized,
 * strictly increasing sequence and out_of_order is "not applied", never "saved"; fenced/expired/
 * session_required stop writing and keep the local position; a retried claim is re-verified through
 * state before it counts; once v2 is chosen nothing here falls back to the legacy route.
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

    /** HTTP status (0 when the server could not be reached) and the decoded JSON object, if any. */
    class Response(val status: Int, val body: Map<String, Any?>?) {
        val ok: Boolean get() = status in 200..299
    }

    enum class Status { IDLE, WRITING, CONFLICT, FENCED, EXPIRED, STALE, RELEASED, UNSUPPORTED, ERROR, DISPOSED }
    enum class Mode(val wire: String) { OPEN("open"), TAKEOVER("takeover") }

    /** What the UI shows: who holds progress (plain text) and the generation a takeover must quote. */
    data class Conflict(val reason: String, val generation: Long, val device: String?)
    data class ClaimResult(val ok: Boolean, val reason: String?, val generation: Long, val device: String?)
    data class SendResult(val ok: Boolean, val reason: String?, val ignored: Boolean = false, val sequence: Long = 0)
    data class StateResult(val supported: Boolean, val reason: String?, val generation: Long, val active: Boolean, val device: String?)

    private class Fence(val generation: Long, val token: String, val expires: Double)
    private class Ticket(val epoch: Long, val generation: Long)
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
    private var sequence = 0L
    private var observedGeneration = 0L
    private val sends = Mutex()
    private val listeners = mutableListOf<(Status) -> Unit>()

    fun onChange(listener: (Status) -> Unit) { listeners += listener }

    private fun set(next: Status, nextConflict: Conflict?) {
        status = next
        conflict = nextConflict
        listeners.toList().forEach { it(next) }
    }

    private fun live(ticket: Ticket) = ticket.epoch == epoch && status != Status.DISPOSED

    /** Only a 2xx with a JSON object body is a success; everything else is classified, never trusted. */
    private fun classify(r: Response): Classified {
        val body = r.body
        return when {
            r.status == 0 -> Classified("network", body, 0)
            r.status == 503 || body?.get("reason") == "disabled" -> Classified("disabled", body, r.status)
            r.status == 409 && body?.get("reason") is String -> Classified("conflict", body, r.status)
            r.ok && body != null -> Classified("ok", body, r.status)
            else -> Classified("http", body, r.status)
        }
    }

    private fun fail(ticket: Ticket, reason: String, http: Int = 0): SendResult {
        if (!live(ticket)) return SendResult(false, "stale_response")
        lastError = if (http > 0) "${reason}_$http" else reason
        unsaved = true
        set(if (fence != null) Status.WRITING else Status.ERROR, conflict)   // a chosen v2 never falls back to legacy
        return SendResult(false, lastError)
    }

    /** Reads who holds progress without claiming anything; remembers the generation for the next open. */
    suspend fun state(): StateResult {
        val ticket = Ticket(epoch, fence?.generation ?: 0)
        val r = transport.get("$STATE?media_key=" + URLEncoder.encode(mediaKey, "UTF-8"))
        val c = classify(r)
        if (!live(ticket)) return StateResult(false, "stale_response", generation, false, null)
        if (c.kind == "disabled") { set(if (fence != null) Status.ERROR else Status.UNSUPPORTED, null); return StateResult(false, "disabled", generation, false, null) }
        val body = c.body
        val gen = safeLong(body?.get("generation"))
        if (c.kind != "ok" || body == null || body["media_key"] != mediaKey || gen == null || body["active"] !is Boolean) {
            return StateResult(false, if (c.kind == "ok") "invalid_state" else c.kind, generation, false, null)
        }
        val active = body["active"] == true
        observedGeneration = gen
        val label = if (active) sanitizeLabel(body["device_id"] as? String) else null
        listeners.toList().forEach { it(status) }
        return StateResult(true, null, gen, active, label)
    }

    /** OPEN on an explicit Play/Resume; TAKEOVER only from a visible user action with the generation shown. */
    suspend fun claim(mode: Mode = Mode.OPEN, expectedGeneration: Long? = null): ClaimResult {
        require(mode != Mode.TAKEOVER || expectedGeneration != null) { "takeover needs the generation the user saw" }
        val expected = expectedGeneration ?: generation
        val requestId = random(16)
        val token = random(32)
        require(requestId.length >= 16 && token.length in 32..128) { "random identifiers are too short" }
        val body = mapOf(
            "media_key" to mediaKey, "session" to session, "request_id" to requestId, "writer_token" to token,
            "device_id" to device, "expected_generation" to expected, "mode" to mode.wire,
        )
        val ticket = Ticket(++epoch, 0)             // supersedes every earlier claim and every packet of the old fence
        fence = null; sequence = 0; lastError = null
        var attempt = 0
        var r: Response
        do {                                         // a network failure retries the SAME packet, never a second unknown claim
            attempt++
            r = transport.post(CLAIM, body, false)
        } while (r.status == 0 && attempt <= retries && live(ticket))
        if (!live(ticket)) return ClaimResult(false, "superseded", generation, null)
        val c = classify(r)
        val b = c.body
        if (c.kind == "conflict" || (c.kind == "ok" && b?.get("ok") == false)) {
            val active = b?.get("active") == true
            val conflictNow = Conflict(b?.get("reason") as? String ?: "conflict", safeLong(b?.get("generation")) ?: 0L, if (active) sanitizeLabel(b?.get("device_id") as? String) else null)
            observedGeneration = conflictNow.generation
            set(Status.CONFLICT, conflictNow)
            return ClaimResult(false, conflictNow.reason, conflictNow.generation, conflictNow.device)
        }
        if (c.kind != "ok" || b == null) { fail(ticket, if (c.kind == "disabled") "disabled" else c.kind, c.http); return ClaimResult(false, lastError, generation, null) }
        val gen = safeLong(b["generation"])
        val expires = (b["expires"] as? Number)?.toDouble()
        if (b["ok"] != true || b["media_key"] != mediaKey || gen == null || gen < 1 || expires == null || expires.isNaN()) {
            fail(ticket, "invalid_claim"); return ClaimResult(false, "invalid_claim", generation, null)
        }
        if (attempt > 1) {                           // a replayed result is history: confirm the writer is still ours
            val current = state()
            if (!live(ticket) || !current.supported || !current.active || current.generation != gen) {
                if (live(ticket)) { fence = null; set(Status.STALE, Conflict("stale", current.generation, current.device)) }
                return ClaimResult(false, "verify_failed", current.generation, current.device)
            }
        }
        fence = Fence(gen, token, expires)
        observedGeneration = gen
        unsaved = false
        set(Status.WRITING, null)
        return ClaimResult(true, null, gen, null)
    }

    private suspend fun send(path: String, extra: Map<String, Any?>, keepalive: Boolean): SendResult = sends.withLock {
        val f = fence
        if (status != Status.WRITING || f == null) return@withLock SendResult(false, if (status == Status.IDLE) "no_claim" else status.name.lowercase())
        if (sequence >= MAX_SEQUENCE) return@withLock SendResult(false, "sequence_exhausted")
        val ticket = Ticket(epoch, f.generation)
        val sent = ++sequence
        val body = mapOf("media_key" to mediaKey, "session" to session, "generation" to f.generation, "writer_token" to f.token, "sequence" to sent) + extra
        val r = transport.post(path, body, keepalive)
        val c = classify(r)
        val b = c.body
        val current = fence
        if (!live(ticket) || current == null || current.generation != ticket.generation) return@withLock SendResult(false, "stale_response")
        if (c.kind == "disabled") return@withLock fail(ticket, "disabled")
        if (c.kind == "conflict" || (c.kind == "ok" && (b?.get("ignored") == true || b?.get("ok") == false))) {
            val reason = b?.get("reason") as? String ?: "rejected"
            if (reason == "fenced" || reason == "expired" || reason == "session_required") {
                fence = null; unsaved = true                 // another writer owns progress now (or ours lapsed): keep the local position
                val active = b?.get("active") == true
                set(if (reason == "expired") Status.EXPIRED else Status.FENCED,
                    Conflict(reason, safeLong(b?.get("generation")) ?: ticket.generation, if (active) sanitizeLabel(b?.get("device_id") as? String) else null))
                return@withLock SendResult(false, reason)
            }
            if (b?.get("ignored") == true) return@withLock SendResult(false, reason, ignored = true)   // out_of_order: not applied
            return@withLock fail(ticket, reason)
        }
        if (c.kind != "ok" || b == null) return@withLock fail(ticket, c.kind, c.http)
        if (b["ok"] != true) return@withLock fail(ticket, "invalid_reply")
        unsaved = false; lastError = null
        listeners.toList().forEach { it(status) }
        SendResult(true, null, sequence = sent)
    }

    suspend fun progress(position: Double, duration: Double, playing: Boolean, keepalive: Boolean = false): SendResult =
        send(PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing), keepalive)

    suspend fun heartbeat(): SendResult = send(HEARTBEAT, emptyMap(), false)

    suspend fun release(keepalive: Boolean = false): SendResult {
        val r = send(RELEASE, emptyMap(), keepalive)
        if (r.ok) { fence = null; set(Status.RELEASED, null) }
        return r
    }

    /** After this, every pending answer is inert; a new media or instance starts from a new client. */
    fun dispose() { epoch++; fence = null; set(Status.DISPOSED, conflict) }

    companion object {
        const val STATE = "/api/playback-sessions/state"
        const val CLAIM = "/api/playback-sessions/claim"
        const val PROGRESS = "/api/playback-sessions/progress"
        const val HEARTBEAT = "/api/playback-sessions/heartbeat"
        const val RELEASE = "/api/playback-sessions/release"
        const val LABEL_MAX = 32
        private const val MAX_SEQUENCE = 2147483647L

        /** The bootstrap advertises the capability; health or a state 503 never do. */
        fun negotiated(capabilities: Map<String, Any?>?): Boolean {
            val w = capabilities?.get("playback_writer") as? Map<*, *> ?: return false
            return w["protocol"] == "playback-writer/v2" && w["enabled"] == true
        }

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
