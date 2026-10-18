package com.fanyu.tv.player

import com.fanyu.tv.data.ApiError
import com.fanyu.tv.player.PlaybackWriterClient.Mode
import com.fanyu.tv.player.PlaybackWriterClient.Response
import com.fanyu.tv.player.PlaybackWriterClient.Status
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.job
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.*
import org.junit.Test

/** The activity's progress-writing side with a cancellable lifecycle scope, synchronous player callbacks and gated transports. */
class WriterCoordinatorTest {
    private class Gate : PlaybackWriterClient.Transport {
        val sent = mutableListOf<String>()
        val bodies = mutableListOf<Map<String, Any?>>()
        val gates = mutableListOf<CompletableDeferred<Response>>()
        var immediateClaim: Response? = null   // a claim answered without suspending, so a test can re-claim ahead of a queued job
        override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response {
            sent += path; bodies += body
            if (path == PlaybackWriterClient.CLAIM) immediateClaim?.let { return it }
            return CompletableDeferred<Response>().also { gates += it }.await()
        }
        override suspend fun get(path: String): Response { sent += path; return PlaybackWriterClientTest.state(0) }
        suspend fun until(n: Int) { while (gates.size < n) yield() }
    }

    private class Ui : WriterCoordinator.Ui {
        val events = mutableListOf<String>()
        override fun render() { events += "render" }
        override fun status(text: String) { events += "status:$text" }
        override fun saveNow() { events += "save" }
    }

    /** Legacy posts wait on a gate each; completing one exceptionally is the server's answer. */
    private class LegacyGate : WriterCoordinator.Legacy {
        val positions = mutableListOf<Double>()
        val gates = mutableListOf<CompletableDeferred<Unit>>()
        override suspend fun post(media: String, position: Double, duration: Double, playing: Boolean) {
            positions += position
            CompletableDeferred<Unit>().also { gates += it }.await()
        }
    }

    private var seed = 0
    private fun client(t: PlaybackWriterClient.Transport) =
        PlaybackWriterClient(KEY, "session-aaaaaaaaaaaaaaaa", "电视播放器 7F3A", t, { n -> "r" + (++seed).toString().padStart(3, '0') + "x".repeat(n) }, 0)
    private fun ok(generation: Long) =
        Response(200, mapOf("ok" to true, "media_key" to KEY, "requested_media_key" to KEY, "generation" to generation, "expires" to 999.0))
    private fun applied(generation: Long, sequence: Long) = Response(200, mapOf("ok" to true, "generation" to generation, "sequence" to sequence))
    private fun CoroutineScope.coordinator(ui: Ui, legacy: LegacyGate = LegacyGate()) =
        WriterCoordinator(CoroutineScope(coroutineContext + Job()), ui, legacy).also { it.media = KEY }
    private val CoroutineScope.lifecycle: Job get() = coroutineContext.job

    companion object {
        const val KEY = PlaybackWriterClientTest.KEY
        val V2 = mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to true))
    }

    @Test fun destroyingTheActivityDuringAClaimWritesNothingAndKeepsTheCancellation() = runBlocking<Unit> {
        val lifecycle = CoroutineScope(coroutineContext + Job())
        val t = Gate(); val ui = Ui(); val co = WriterCoordinator(lifecycle, ui, LegacyGate()).also { it.media = KEY }
        val c = client(t); co.open(V2) { c }; yield()                                   // the state read is answered at once
        co.intent.arm(KEY, false); co.playingChanged(true); t.until(1)                   // the user's Play: the claim is in flight
        ui.events.clear()
        lifecycle.cancel(); co.dispose()                                                 // onDestroy
        lifecycle.lifecycle.join()                                                       // the claim coroutine ended by cancellation
        t.gates[0].complete(ok(1)); yield()
        assertEquals(emptyList<String>(), ui.events)                                     // no status text, no failure, no save
        assertEquals(Status.DISPOSED, c.status); assertEquals(2, t.sent.size)
    }

    @Test fun aLateClaimResultOfAReplacedWriterTouchesNothing() = runBlocking<Unit> {
        val t1 = Gate(); val ui = Ui(); val co = coordinator(ui)
        val c1 = client(t1); co.open(V2) { c1 }; yield()
        co.claim(Mode.OPEN, null); t1.until(1)
        val t2 = Gate(); val c2 = client(t2); co.open(V2) { c2 }; yield(); ui.events.clear()   // another writer is live now
        t1.gates[0].complete(ok(1)); yield()
        assertEquals(emptyList<String>(), ui.events); assertEquals(Status.DISPOSED, c1.status)
        co.claim(Mode.OPEN, null); t2.until(1)                                           // no flag of the old writer is stuck
        t2.gates[0].complete(ok(1)); yield()
        assertTrue(ui.events.contains("save")); assertTrue(ui.events.none { it.startsWith("status:") }); assertEquals(Status.WRITING, c2.status)
    }

    @Test fun onlyAConsumedPlayIntentClaimsAndBufferingResumesNever() = runBlocking<Unit> {
        val t = Gate(); val ui = Ui(); val co = coordinator(ui)
        val c = client(t); co.open(V2) { c }; yield()
        co.playingChanged(true)                                                          // autoplay: nothing was armed
        co.intent.arm(KEY, true); co.playingChanged(false); co.playingChanged(true)      // "play" while already playing arms nothing
        assertEquals(0, t.gates.size)
        co.intent.arm(KEY, false); co.playingChanged(false); co.playingChanged(true)     // the real start after buffering consumes it
        t.until(1); assertEquals(PlaybackWriterClient.CLAIM, t.sent[1])
        t.gates[0].complete(ok(1)); yield(); assertEquals(Status.WRITING, c.status); assertTrue(ui.events.contains("save"))
        co.playingChanged(false); co.playingChanged(true)                                // a buffering resume never claims again
        co.intent.arm(KEY, false); co.media = "other"; co.playingChanged(true)           // playback of another media discards it
        co.media = KEY; co.playingChanged(true)
        assertEquals(1, t.gates.size)
    }

    @Test fun theFinalSnapshotLeavesAfterDestroyAndClosesAnInFlightClaim() = runBlocking<Unit> {
        val app = CoroutineScope(coroutineContext + Job())                               // outlives every activity
        val lifecycle = CoroutineScope(coroutineContext + Job())
        val t = Gate(); val ui = Ui(); val co = WriterCoordinator(lifecycle, ui, LegacyGate()).also { it.media = KEY }
        val c = client(t); co.open(V2) { c }; yield()
        co.claim(Mode.OPEN, null); t.until(1); t.gates[0].complete(ok(1)); yield(); assertEquals(Status.WRITING, c.status)
        co.final(30.0, 100.0, false, app); lifecycle.cancel(); co.dispose()             // onStop, then onDestroy
        t.until(2); assertEquals(PlaybackWriterClient.PROGRESS, t.sent[2]); assertEquals(30.0, t.bodies[1]["position"])
        t.gates[1].complete(applied(1, 1)); yield()
        assertEquals(Status.DISPOSED, c.status)
        for (stop in listOf<(WriterCoordinator) -> Unit>({ it.final(5.0, 9.0, false, app) }, { it.stopWriting() })) {
            val t2 = Gate(); val ui2 = Ui(); val co2 = coordinator(ui2)
            val c2 = client(t2); co2.open(V2) { c2 }; yield()
            co2.claim(Mode.OPEN, null); t2.until(1)                                      // the claim is in flight when the activity stops
            stop(co2)
            t2.gates[0].complete(ok(1)); yield()
            assertNotEquals(Status.WRITING, c2.status); assertFalse(ui2.events.contains("save")); assertEquals(1, t2.gates.size)
        }
        app.cancel()
    }

    @Test fun queuedOperationsCarryTheIdentityOfTheirCall() = runBlocking<Unit> {   // owner round-22 group 2: launches in this scope run only when the test yields
        val t = Gate(); val ui = Ui(); val co = coordinator(ui)
        val c = client(t); co.open(V2) { c }; yield()
        co.claim(Mode.OPEN, null)                                                        // queued, not started
        co.stopWriting()                                                                 // onStop before the queue ran
        yield()
        assertEquals(1, t.sent.size); assertFalse(ui.events.contains("save"))            // the claim never left
        co.claim(Mode.OPEN, null); t.until(1); t.gates[0].complete(ok(1)); yield()       // and the flag is free for a real one
        assertEquals(Status.WRITING, c.status)
        co.save(10.0, 100.0, true)                                                       // queued under generation 1
        t.immediateClaim = ok(2); assertTrue(c.claim().ok); assertEquals(2L, c.generation)   // an explicit re-claim before the queue ran
        yield()
        assertEquals(3, t.sent.size); assertEquals(PlaybackWriterClient.CLAIM, t.sent.last())   // position 10 never left, under no generation
        co.save(11.0, 100.0, true); t.until(2)
        assertEquals(2L, t.bodies.last()["generation"]); assertEquals(1L, t.bodies.last()["sequence"]); assertEquals(11.0, t.bodies.last()["position"])
        t.gates[1].complete(applied(2, 1)); yield()
        for (close in listOf<(WriterCoordinator) -> Unit>({ it.final(1.0, 2.0, false, this) }, { it.dispose() }, { it.open(V2) { client(Gate()) } })) {
            val t2 = Gate(); val ui2 = Ui(); val co2 = coordinator(ui2)
            val c2 = client(t2); co2.open(V2) { c2 }; yield()
            co2.claim(Mode.OPEN, null)                                                   // queued
            close(co2); yield()
            assertEquals(1, t2.sent.size); assertFalse(ui2.events.contains("save"))      // closed before it could start
        }
    }

    @Test fun withoutTheCapabilityNoV2RequestLeavesAndTheLegacyRouteStopsAtADurableFence() = runBlocking<Unit> {
        val legacy = LegacyGate(); val ui = Ui(); val co = coordinator(ui, legacy)
        co.open(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to "true"))) { throw AssertionError("no writer without the capability") }
        co.intent.arm(KEY, false); co.playingChanged(true)                               // nothing to claim with
        assertNull(co.writer); assertEquals(emptyList<String>(), ui.events)
        co.save(10.0, 100.0, true); yield(); co.save(11.0, 100.0, true); yield()
        assertEquals(listOf(10.0, 11.0), legacy.positions)
        legacy.gates[1].completeExceptionally(ApiError(409, "需要会话", mapOf("reason" to "session_required"))); yield()
        assertTrue(co.legacyFenced); assertEquals("status:" + WriterCoordinator.LEGACY_FENCED, ui.events.last())
        legacy.gates[0].complete(Unit); yield()                                          // the older save succeeds late
        assertEquals("status:" + WriterCoordinator.LEGACY_FENCED, ui.events.last())      // the fence stays visible
        co.save(12.0, 100.0, true); yield()
        assertEquals(2, legacy.positions.size)                                           // nothing more leaves
        val plain = LegacyGate(); val ui2 = Ui(); val co2 = coordinator(ui2, plain)      // an ordinary failure is visible, not a fence
        co2.save(1.0, 2.0, true); yield(); plain.gates[0].completeExceptionally(ApiError(0, "连不上番屿服务")); yield()
        assertEquals("status:" + WriterCoordinator.FAILED, ui2.events.last()); assertFalse(co2.legacyFenced)
        co2.save(2.0, 2.0, true); yield(); plain.gates[1].complete(Unit); yield()
        assertEquals("status:", ui2.events.last())
    }

    @Test fun aNegotiatedWriterWritesOnlyWhileWritingAndNeverFallsBackToLegacy() = runBlocking<Unit> {
        val legacy = LegacyGate(); val t = Gate(); val ui = Ui(); val co = coordinator(ui, legacy)
        val c = client(t); co.open(V2) { c }; yield()
        co.save(1.0, 2.0, true); yield()                                                 // IDLE: no packet on either route
        assertEquals(1, t.sent.size); assertEquals(0, legacy.positions.size)
        co.claim(Mode.OPEN, null); t.until(1); t.gates[0].complete(ok(1)); yield()
        ui.events.clear()
        co.save(3.0, 9.0, true); t.until(2); assertEquals(PlaybackWriterClient.PROGRESS, t.sent[2]); assertEquals(3.0, t.bodies[1]["position"])
        t.gates[1].complete(Response(503, mapOf("reason" to "disabled"))); yield()          // the capability vanished: halted, still no legacy
        assertEquals(Status.HALTED, c.status); assertEquals(listOf("render", "render"), ui.events)
        co.save(4.0, 9.0, true); yield(); co.intent.arm(KEY, false); co.playingChanged(true); yield()
        assertEquals(3, t.sent.size); assertEquals(0, legacy.positions.size)
    }
}
