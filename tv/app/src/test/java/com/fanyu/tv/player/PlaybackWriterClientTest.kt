package com.fanyu.tv.player

import com.fanyu.tv.player.PlaybackWriterClient.Mode
import com.fanyu.tv.player.PlaybackWriterClient.Response
import com.fanyu.tv.player.PlaybackWriterClient.Status
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class PlaybackWriterClientTest {
    private class Call(val path: String, val body: Map<String, Any?>, val keepalive: Boolean)

    /** Scripted transport: answers come from [answer], every request is recorded. */
    private class Fake(val answer: (String, Map<String, Any?>, Int) -> Response) : PlaybackWriterClient.Transport {
        val calls = mutableListOf<Call>()
        var stateAnswer = Response(200, mapOf("generation" to 0L, "active" to false))
        override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response {
            calls += Call(path, body, keepalive)
            return answer(path, body, calls.size)
        }
        override suspend fun get(path: String): Response { calls += Call(path, emptyMap(), false); return stateAnswer }
    }

    private var seed = 0
    private fun client(fake: Fake) = PlaybackWriterClient("media-1", "session-a", "TV · 卧室", fake) { n -> "r" + (++seed) + "x".repeat(n) }
    private fun ok(generation: Long, expires: Double = 999.0) = Response(200, mapOf("ok" to true, "media_key" to "media-1", "generation" to generation, "expires" to expires))

    @Test fun openingNeverClaimsAndPlayClaimsWithFreshIdentity() = runBlocking {
        val fake = Fake { _, _, _ -> ok(1) }
        val c = client(fake)
        assertEquals(Status.IDLE, c.status)
        assertTrue(c.state())
        assertEquals(listOf("/api/playback-sessions/state?media_key=media-1"), fake.calls.map { it.path })
        val r = c.claim()
        assertTrue(r.ok); assertEquals(1L, r.generation); assertEquals(Status.WRITING, c.status)
        val claim = fake.calls[1].body
        assertEquals("open", claim["mode"]); assertEquals(0L, claim["expected_generation"])
        assertTrue((claim["request_id"] as String).startsWith("r")); assertEquals(32 + 2, (claim["writer_token"] as String).length)
        assertEquals("TV · 卧室", claim["device_id"])
    }

    @Test fun liveWriterElsewhereIsAVisibleConflictAndTakeoverNeedsTheSeenGeneration() = runBlocking {
        val fake = Fake { _, _, n -> if (n == 1) Response(409, mapOf("ok" to false, "reason" to "takeover_required", "generation" to 4L, "active" to true, "device_id" to "Web · 客厅")) else ok(5) }
        val c = client(fake)
        val first = c.claim()
        assertFalse(first.ok); assertEquals("takeover_required", first.reason); assertEquals(4L, first.generation); assertEquals("Web · 客厅", first.device)
        assertEquals(Status.CONFLICT, c.status); assertEquals("Web · 客厅", c.conflict?.device)
        assertThrows(IllegalArgumentException::class.java) { runBlocking { c.claim(Mode.TAKEOVER) } }
        val second = c.claim(Mode.TAKEOVER, 4)
        assertTrue(second.ok); assertEquals(5L, second.generation); assertEquals(Status.WRITING, c.status)
        assertEquals("takeover", fake.calls[1].body["mode"]); assertEquals(4L, fake.calls[1].body["expected_generation"])
        assertNotEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"])
        assertNotEquals(fake.calls[0].body["writer_token"], fake.calls[1].body["writer_token"])
    }

    @Test fun aStaleClaimAnswerCannotTakeOverAfterANewerClaimStarted() = runBlocking {
        val gates = mutableListOf<CompletableDeferred<Response>>()
        val fake = Fake { _, _, _ -> CompletableDeferred<Response>().also { gates += it }.let { runBlocking { it.await() } } }
        val c = client(fake)
        val slow = async { c.claim() }
        while (gates.size < 1) kotlinx.coroutines.yield()
        val fast = async { c.claim() }
        while (gates.size < 2) kotlinx.coroutines.yield()
        gates[1].complete(ok(2))
        assertEquals(2L, fast.await().generation)
        gates[0].complete(ok(1))   // late answer to the superseded claim
        assertEquals("superseded", slow.await().reason)
        assertEquals(2L, c.generation)
    }

    @Test fun progressHeartbeatAndReleaseShareOneSequenceUnderTheFence() = runBlocking {
        val fake = Fake { path, body, _ -> if (path.endsWith("/claim")) ok(3) else Response(200, mapOf("ok" to true, "generation" to 3L, "sequence" to body["sequence"])) }
        val c = client(fake)
        c.claim()
        c.progress(10.0, 100.0, true)
        c.heartbeat()
        c.progress(20.0, 100.0, false)
        c.release(keepalive = true)
        val packets = fake.calls.drop(1)
        assertEquals(listOf(1L, 2L, 3L, 4L), packets.map { it.body["sequence"] })
        assertTrue(packets.all { it.body["generation"] == 3L && it.body["session"] == "session-a" && it.body["writer_token"] == fake.calls[0].body["writer_token"] })
        assertEquals(true, packets[0].body["playing"]); assertEquals(false, packets[2].body["playing"])
        assertTrue(packets[3].keepalive); assertEquals(Status.RELEASED, c.status)
    }

    @Test fun fencedStopsWritingKeepsTheConflictVisibleAndNeedsAnExplicitReclaim() = runBlocking {
        var n = 0L
        val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(++n) else Response(409, mapOf("ok" to false, "ignored" to true, "reason" to "fenced", "generation" to 7L, "device_id" to "Web · 客厅")) }
        val c = client(fake)
        c.claim()
        val r = c.progress(1.0, 2.0, true)
        assertFalse(r.ok); assertEquals("fenced", r.reason); assertEquals(Status.FENCED, c.status); assertEquals("Web · 客厅", c.conflict?.device)
        assertEquals("fenced", c.heartbeat().reason)            // no background retries once fenced
        assertEquals(2, fake.calls.size)
        c.claim(Mode.TAKEOVER, 7)
        assertEquals(7L, fake.calls[2].body["expected_generation"])
        c.progress(1.0, 2.0, true)
        assertEquals(1L, fake.calls[3].body["sequence"])         // a fresh fence restarts the sequence
    }

    @Test fun duplicatesAreHarmlessOtherRejectionsSurfaceAndDisabledMeansUnsupported() = runBlocking {
        val answers = ArrayDeque(listOf(ok(1), Response(409, mapOf("ok" to false, "ignored" to true, "reason" to "out_of_order")),
            Response(409, mapOf("ok" to false, "reason" to "generation_moved")), Response(503, mapOf("reason" to "disabled"))))
        val fake = Fake { _, _, _ -> answers.removeFirst() }
        val c = client(fake)
        c.claim()
        val dup = c.heartbeat(); assertTrue(dup.ok); assertTrue(dup.ignored); assertEquals("out_of_order", dup.reason); assertEquals(Status.WRITING, c.status)
        val moved = c.heartbeat(); assertFalse(moved.ok); assertEquals("generation_moved", moved.reason)
        val off = c.heartbeat(); assertFalse(off.ok); assertEquals("disabled", off.reason); assertEquals(Status.UNSUPPORTED, c.status)
        assertEquals("unsupported", c.progress(0.0, 1.0, false).reason)
    }

    @Test fun anOlderServerLeavesTheClientUnsupportedBeforeAnyClaim() = runBlocking {
        val fake = Fake { _, _, _ -> throw AssertionError("must not be called") }
        fake.stateAnswer = Response(503, mapOf("reason" to "disabled"))
        val c = client(fake)
        assertFalse(c.state()); assertEquals(Status.UNSUPPORTED, c.status); assertEquals(1, fake.calls.size)
    }

    @Test fun listenersSeeEveryTransition() = runBlocking {
        val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(1) else Response(409, mapOf("ok" to false, "ignored" to true, "reason" to "expired")) }
        val c = client(fake)
        val seen = mutableListOf<Status>()
        c.onChange { seen += it }
        c.claim(); c.progress(1.0, 2.0, true)
        assertEquals(listOf(Status.WRITING, Status.EXPIRED), seen)
    }
}
