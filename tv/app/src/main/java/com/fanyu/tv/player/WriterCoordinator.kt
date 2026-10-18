package com.fanyu.tv.player

import com.fanyu.tv.data.ApiError
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/**
 * The player activity's progress-writing side, pure Kotlin so lifecycle cancellation and synchronous
 * player callbacks are covered by JVM tests. Without the negotiated capability no v2 request leaves and
 * the legacy route is used until it meets a durable fence; with it, one live writer per opened media
 * writes only while WRITING and never falls back. An OPEN claim needs a consumed one-shot play intent, a
 * TAKEOVER the visible action; the final snapshot is captured on the caller's thread and sent by a scope
 * that outlives the activity. A UI write happens only for the writer that is still live, and
 * cancellation (the activity going away) is never swallowed: a cancelled coroutine writes nothing.
 */
class WriterCoordinator(private val scope: CoroutineScope, private val ui: Ui, private val legacy: Legacy) {
    interface Ui {
        /** Draw the live writer's status; with no writer, the legacy status is left alone unless fenced. */
        fun render()
        /** A plain status line: a connection failure or the legacy route's outcome ("" once saved). */
        fun status(text: String)
        /** A regular save right after a successful claim. */
        fun saveNow()
    }

    /** The legacy progress write; an [ApiError] carries the server's structured 409 reason. */
    interface Legacy {
        suspend fun post(media: String, position: Double, duration: Double, playing: Boolean)
    }

    /** The media whose progress is written; set once it is known. */
    @Volatile var media: String? = null
    var writer: PlaybackWriterClient? = null
        private set
    /** The legacy route met a durable writer fence: it stops writing until a new session is negotiated. */
    var legacyFenced = false
        private set
    val intent = PlayIntent()   // one-shot, bound to a media, consumed by its first real playback
    private var claiming = false
    private var ops = 0L        // bumped by every action that closes queued operations: open, final, stop, dispose

    /** Opens a writer only when the bootstrap advertised the capability; any previous writer is disposed. */
    fun open(capabilities: Map<String, Any?>?, make: () -> PlaybackWriterClient) {
        if (!PlaybackWriterClient.negotiated(capabilities)) return   // legacy progress stays untouched
        val client = make()
        writer?.dispose()
        writer = client
        claiming = false
        ops++
        client.onChange { if (writer === client) ui.render() }
        ui.render()
        scope.launch {
            try { client.state() }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (writer === client) ui.status(FAILED) }
            if (writer === client) ui.render()
        }
    }

    /** Autoplay and buffering resumes never claim; the explicit Play/Resume for this media is consumed by its first real playback. */
    fun playingChanged(isPlaying: Boolean) {
        if (!isPlaying || !intent.consume(media)) return
        val w = writer ?: return
        if (w.status != PlaybackWriterClient.Status.WRITING) claim(PlaybackWriterClient.Mode.OPEN, null)
    }

    /** Takeover/reacquire from the visible button, with the generation the user saw. */
    fun takeover() {
        val w = writer ?: return
        claim(PlaybackWriterClient.Mode.TAKEOVER, w.conflict?.generation ?: w.generation)
    }

    fun claim(mode: PlaybackWriterClient.Mode, expected: Long?) {
        val w = writer ?: return
        if (claiming) return
        claiming = true
        val op = ops                                   // the identity of this call: closed by a stop/final/dispose/open before it runs
        scope.launch {
            try {
                if (writer !== w || ops != op) return@launch
                val r = w.claim(mode, expected)
                if (writer === w) {   // a late result of a replaced writer touches nothing
                    ui.render()
                    if (r.ok && w.status == PlaybackWriterClient.Status.WRITING) ui.saveNow()
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (writer === w) ui.status(FAILED)
            } finally { if (writer === w) claiming = false }
        }
    }

    /** A regular save: the writer only while WRITING (never the legacy route once v2 was negotiated), else legacy until fenced. */
    fun save(position: Double, duration: Double, playing: Boolean) {
        val id = media ?: return
        val w = writer
        if (w != null) {
            val job = w.progressJob(position, duration, playing) ?: run { ui.render(); return }   // bound NOW to the live fence
            scope.launch {   // the client serializes the sequence and re-checks the identity; transport faults are classified inside, never thrown
                job()
                if (writer === w) ui.render()
            }
            return
        }
        if (legacyFenced) return
        scope.launch {   // cancelled with the activity: no UI callback outlives it
            if (legacyFenced || media != id) return@launch   // re-checked when the queued coroutine actually starts
            try {
                legacy.post(id, position, duration, playing)
                if (!legacyFenced) ui.status("")             // an old success never clears a fence that arrived meanwhile
            } catch (e: ApiError) {                           // visible, never swallowed
                if (PlaybackWriterClient.legacyFenced(e.code, e.details)) { legacyFenced = true; ui.status(LEGACY_FENCED) }
                else if (!legacyFenced) ui.status(FAILED)
            }
        }
    }

    /** The last snapshot, captured now on this thread and sent by [sender] even after the activity is gone; its answer updates nobody. */
    fun final(position: Double, duration: Double, playing: Boolean, sender: CoroutineScope) {
        val w = writer ?: return
        ops++
        val job = w.final(position, duration, playing)
        sender.launch { job() }
        ui.render()
    }

    /** Nothing to snapshot: writing still stops now and an in-flight claim is closed. */
    fun stopWriting() {
        val w = writer ?: return
        ops++
        w.suspendWriting()
        ui.render()
    }

    /** The activity is destroyed: late answers become inert; a final snapshot already captured still goes out. */
    fun dispose() { ops++; writer?.dispose() }

    companion object {
        const val FAILED = "进度未保存：连接失败"
        const val LEGACY_FENCED = "进度未保存：需要新的播放会话或版本支持"
    }
}
