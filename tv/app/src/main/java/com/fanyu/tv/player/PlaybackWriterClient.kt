package com.fanyu.tv.player

import java.net.URLEncoder
import java.security.SecureRandom

/**
 * Explicit playback writer client (docs/PLAYBACK-SESSION-CONTRACT.md), pure Kotlin so the JVM unit
 * tests cover it. Nothing in the player uses it until the server advertises the capability.
 *
 * Rules it enforces for the UI: only an explicit Play/Resume claims; a live writer elsewhere is a
 * visible conflict and a takeover needs a user action, a NEW request id and the generation the user
 * actually saw; a claim answered after a newer claim started never takes over; progress, heartbeat
 * and release share one strictly increasing sequence under one fence; fenced or expired means stop
 * writing, keep the local position and wait for an explicit reclaim; a new claim never replays
 * packets queued under the old fence; a rejected save is reported, never swallowed.
 */
class PlaybackWriterClient(
    private val mediaKey: String,
    private val session: String,
    private val device: String,
    private val transport: Transport,
    private val random: (Int) -> String = ::randomHex,
) {
    interface Transport {
        suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response
        suspend fun get(path: String): Response
    }

    /** HTTP status (0 when the server could not be reached) and the decoded JSON object, if any. */
    class Response(val status: Int, val body: Map<String, Any?>?)

    enum class Status { IDLE, WRITING, CONFLICT, FENCED, EXPIRED, RELEASED, UNSUPPORTED, ERROR }
    enum class Mode(val wire: String) { OPEN("open"), TAKEOVER("takeover") }

    /** What the UI shows: who holds progress (a safe label) and the generation a takeover must quote. */
    data class Conflict(val reason: String, val generation: Long, val device: String?)
    data class ClaimResult(val ok: Boolean, val reason: String?, val generation: Long, val device: String?)
    data class SendResult(val ok: Boolean, val reason: String?, val ignored: Boolean = false, val sequence: Long = 0)

    private class Fence(val generation: Long, val token: String)

    @Volatile var status: Status = Status.IDLE
        private set
    @Volatile var conflict: Conflict? = null
        private set
    val generation: Long get() = fence?.generation ?: conflict?.generation ?: 0L

    private var fence: Fence? = null
    private var sequence = 0L
    private var claimSerial = 0L
    private val listeners = mutableListOf<(Status) -> Unit>()

    fun onChange(listener: (Status) -> Unit) { listeners += listener }

    private fun set(next: Status, next_conflict: Conflict?) {
        status = next
        conflict = next_conflict
        listeners.forEach { it(next) }
    }

    /** True when the server offers the capability; opening a page never claims anything. */
    suspend fun state(): Boolean {
        val r = transport.get("$STATE?media_key=" + URLEncoder.encode(mediaKey, "UTF-8"))
        if (r.status == 503 || r.body?.get("reason") == "disabled") { set(Status.UNSUPPORTED, null); return false }
        check(r.status in 200..299) { "playback state unavailable" }
        return true
    }

    /** OPEN on an explicit Play/Resume; TAKEOVER only from a visible user action with the generation shown. */
    suspend fun claim(mode: Mode = Mode.OPEN, expectedGeneration: Long? = null): ClaimResult {
        require(mode != Mode.TAKEOVER || expectedGeneration != null) { "takeover needs the generation the user saw" }
        val expected = expectedGeneration ?: generation
        val token = random(32)
        val serial = ++claimSerial
        val body = mapOf(
            "media_key" to mediaKey, "session" to session, "request_id" to random(16), "writer_token" to token,
            "device_id" to device, "expected_generation" to expected, "mode" to mode.wire,
        )
        val r = transport.post(CLAIM, body, false)
        if (serial != claimSerial) return ClaimResult(false, "superseded", generation, null)
        if (r.status == 503 || r.body?.get("reason") == "disabled") { set(Status.UNSUPPORTED, null); return ClaimResult(false, "disabled", generation, null) }
        val b = r.body
        if (r.status == 400 || b == null || (r.status !in 200..299 && b["reason"] == null)) {
            set(Status.ERROR, conflict); return ClaimResult(false, "invalid_request", generation, null)
        }
        if (b["ok"] == false) {
            val c = Conflict(b["reason"] as? String ?: "conflict", long(b["generation"]), b["device_id"] as? String)
            set(Status.CONFLICT, c)
            return ClaimResult(false, c.reason, c.generation, c.device)
        }
        // A fresh fence: packets queued under the old one are never replayed.
        fence = Fence(long(b["generation"]), token)
        sequence = 0
        set(Status.WRITING, null)
        return ClaimResult(true, null, long(b["generation"]), null)
    }

    private suspend fun send(path: String, extra: Map<String, Any?>, keepalive: Boolean): SendResult {
        val f = fence
        if (status != Status.WRITING || f == null) return SendResult(false, if (status == Status.IDLE) "no_claim" else status.name.lowercase())
        val body = mapOf("media_key" to mediaKey, "session" to session, "generation" to f.generation, "writer_token" to f.token, "sequence" to ++sequence) + extra
        val r = transport.post(path, body, keepalive)
        val b = r.body
        if (r.status == 503 || b?.get("reason") == "disabled") { set(Status.UNSUPPORTED, null); return SendResult(false, "disabled") }
        val reason = b?.get("reason") as? String
        if (b?.get("ignored") == true && (reason == "fenced" || reason == "expired")) {
            // Another writer owns progress now (or ours lapsed): stop writing, keep the local position.
            fence = null
            val shown = long(b["generation"]).takeIf { it > 0 } ?: f.generation
            set(if (reason == "fenced") Status.FENCED else Status.EXPIRED, Conflict(reason, shown, b["device_id"] as? String))
            return SendResult(false, reason)
        }
        if (b?.get("ignored") == true) return SendResult(true, reason, ignored = true)   // duplicate/older packet: harmless
        if (b == null || b["ok"] == false || r.status !in 200..299) return SendResult(false, reason ?: "rejected")
        return SendResult(true, null, sequence = long(b["sequence"]).takeIf { it > 0 } ?: sequence)
    }

    suspend fun progress(position: Double, duration: Double, playing: Boolean, keepalive: Boolean = false): SendResult =
        send(PROGRESS, mapOf("position" to position, "duration" to duration, "playing" to playing), keepalive)

    suspend fun heartbeat(): SendResult = send(HEARTBEAT, emptyMap(), false)

    suspend fun release(keepalive: Boolean = false): SendResult {
        val r = send(RELEASE, emptyMap(), keepalive)
        if (r.ok || r.reason == "no_claim") { fence = null; if (status == Status.WRITING) set(Status.RELEASED, null) }
        return r
    }

    companion object {
        const val STATE = "/api/playback-sessions/state"
        const val CLAIM = "/api/playback-sessions/claim"
        const val PROGRESS = "/api/playback-sessions/progress"
        const val HEARTBEAT = "/api/playback-sessions/heartbeat"
        const val RELEASE = "/api/playback-sessions/release"

        private fun long(value: Any?): Long = when (value) {
            is Number -> value.toLong()
            is String -> value.toLongOrNull() ?: 0L
            else -> 0L
        }

        fun randomHex(bytes: Int): String {
            val buffer = ByteArray(bytes)
            SecureRandom().nextBytes(buffer)
            return buffer.joinToString("") { "%02x".format(it) }
        }
    }
}
