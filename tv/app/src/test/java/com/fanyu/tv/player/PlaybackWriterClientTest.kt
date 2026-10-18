package com.fanyu.tv.player

import com.fanyu.tv.player.PlaybackWriterClient.Mode
import com.fanyu.tv.player.PlaybackWriterClient.Response
import com.fanyu.tv.player.PlaybackWriterClient.Status
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.*
import org.junit.Test

class PlaybackWriterClientTest {
    private class Call(val path: String, val body: Map<String, Any?>, val keepalive: Boolean)

    /** Scripted transport: [answer] decides each reply (it may suspend), every request is recorded. */
    private class Fake(val answer: suspend (String, Map<String, Any?>, Int) -> Response) : PlaybackWriterClient.Transport {
        val calls = mutableListOf<Call>()
        var stateAnswer = Response(200, mapOf("media_key" to "media-1", "generation" to 0L, "active" to false, "expires" to 0.0, "device_id" to null))
        override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response {
            calls += Call(path, body, keepalive)
            return answer(path, body, calls.size)
        }
        override suspend fun get(path: String): Response { calls += Call(path, emptyMap(), false); return stateAnswer }
    }

    private var seed = 0
    private val session = "session-aaaaaaaaaaaaaaaa"
    private fun client(fake: Fake, retries: Int = 1) =
        PlaybackWriterClient("media-1", session, "电视播放器 7F3A", fake, { n -> "r" + (++seed).toString().padStart(3, '0') + "x".repeat(n) }, retries)
    private fun ok(generation: Long, expires: Double = 999.0) = Response(200, mapOf("ok" to true, "media_key" to "media-1", "generation" to generation, "expires" to expires))
    private fun applied(sequence: Long) = Response(200, mapOf("ok" to true, "generation" to 1L, "sequence" to sequence))
    private fun conflict(reason: String, vararg extra: Pair<String, Any?>) = Response(409, mapOf("error" to "conflict", "ok" to false, "reason" to reason) + extra)
    private fun gate() = CompletableDeferred<Response>()

    @Test fun negotiationOnlyThroughTheBootstrapAndLabelsAreSanitized() {
        assertTrue(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to true))))
        assertFalse(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to "true"))))
        assertFalse(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v1", "enabled" to true))))
        assertFalse(PlaybackWriterClient.negotiated(null)); assertFalse(PlaybackWriterClient.negotiated(emptyMap()))
        assertEquals("电视播放器 <b>X</b>", PlaybackWriterClient.sanitizeLabel(" 电视播放器 <b>X</b>\u0007​ "))
        assertEquals(32, PlaybackWriterClient.sanitizeLabel("x".repeat(40))!!.codePointCount(0, 32))
        assertNull(PlaybackWriterClient.sanitizeLabel("\u0000‎")); assertNull(PlaybackWriterClient.sanitizeLabel(null))
        assertEquals("电视播放器 ABAB", PlaybackWriterClient.deviceLabel("电视播放器") { n -> "ab".repeat(n) })
    }

    @Test fun weakIdentitiesAreRefused() = runBlocking {
        val fake = Fake { _, _, _ -> ok(1) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient("media-1", "short", "电视播放器", fake) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient("media-1", session, "\u0000", fake) }
        val weak = PlaybackWriterClient("media-1", session, "电视播放器", fake, { "ab" })
        assertThrows(IllegalArgumentException::class.java) { runBlocking { weak.claim() } }
        assertEquals(0, fake.calls.size)
    }

    @Test fun readingStateNeverClaimsAndRemembersTheServerGeneration() = runBlocking {
        val fake = Fake { _, _, _ -> ok(8) }
        fake.stateAnswer = Response(200, mapOf("media_key" to "media-1", "generation" to 7L, "active" to true, "expires" to 5.0, "device_id" to "网页播放器 1A2B"))
        val c = client(fake)
        val s = c.state()
        assertTrue(s.supported); assertEquals(7L, s.generation); assertTrue(s.active); assertEquals("网页播放器 1A2B", s.device)
        assertEquals(Status.IDLE, c.status); assertEquals(7L, c.generation)
        c.claim()
        assertEquals(7L, fake.calls[1].body["expected_generation"])      // owner finding 3: not 0
        assertEquals("open", fake.calls[1].body["mode"])
    }

    @Test fun anExplicitPlayClaimsWithFreshIdentifiers() = runBlocking {
        val fake = Fake { _, _, _ -> ok(1) }
        val c = client(fake)
        val r = c.claim()
        assertTrue(r.ok); assertEquals(1L, r.generation); assertEquals(Status.WRITING, c.status)
        val claim = fake.calls[0].body
        assertEquals("电视播放器 7F3A", claim["device_id"]); assertTrue((claim["request_id"] as String).length >= 16)
        assertTrue((claim["writer_token"] as String).length >= 32); assertEquals(0L, claim["expected_generation"])
    }

    @Test fun onlyAValid2xxSucceeds() = runBlocking {
        val answers = ArrayDeque(listOf(Response(500, null), Response(403, emptyMap()), Response(200, mapOf("ok" to true)),
            Response(200, mapOf("ok" to true, "media_key" to "other", "generation" to 1L, "expires" to 1.0)), ok(1), Response(404, mapOf("detail" to "Not Found"))))
        val fake = Fake { _, _, _ -> answers.removeFirst() }
        val c = client(fake, retries = 0)
        assertEquals("http_500", c.claim().reason); assertEquals(Status.ERROR, c.status)
        assertEquals("http_403", c.claim().reason)
        assertEquals("invalid_claim", c.claim().reason)
        assertEquals("invalid_claim", c.claim().reason)
        assertTrue(c.claim().ok)
        val p = c.progress(1.0, 2.0, true)
        assertFalse(p.ok); assertEquals("http_404", p.reason)                  // owner finding 2: not a success
        assertEquals(Status.WRITING, c.status); assertTrue(c.unsaved); assertEquals("http_404", c.lastError)
    }

    @Test fun liveWriterElsewhereIsAVisibleConflictAndTakeoverNeedsTheSeenGeneration() = runBlocking {
        val fake = Fake { _, _, n -> if (n == 1) conflict("takeover_required", "generation" to 4L, "active" to true, "device_id" to "网页播放器 1A2B") else ok(5, 50.0) }
        val c = client(fake)
        val first = c.claim()
        assertFalse(first.ok); assertEquals("takeover_required", first.reason); assertEquals(4L, first.generation); assertEquals("网页播放器 1A2B", first.device)
        assertEquals(Status.CONFLICT, c.status); assertEquals("网页播放器 1A2B", c.conflict?.device)
        assertThrows(IllegalArgumentException::class.java) { runBlocking { c.claim(Mode.TAKEOVER) } }
        val second = c.claim(Mode.TAKEOVER, 4)
        assertTrue(second.ok); assertEquals(5L, second.generation); assertEquals(Status.WRITING, c.status)
        assertEquals("takeover", fake.calls[1].body["mode"]); assertEquals(4L, fake.calls[1].body["expected_generation"])
        assertNotEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"])
        assertNotEquals(fake.calls[0].body["writer_token"], fake.calls[1].body["writer_token"])
    }

    @Test fun lateAnswersOfOlderHeartbeatReleaseOrClaimNeverTouchTheCurrentFence() = runBlocking {
        val gates = mutableListOf<CompletableDeferred<Response>>()
        val fake = Fake { _, _, _ -> gate().also { gates += it }.await() }
        val c = client(fake)
        val first = async { c.claim() }; while (gates.size < 1) yield(); gates[0].complete(ok(1)); assertEquals(1L, first.await().generation)
        val oldBeat = async { c.heartbeat() }; while (gates.size < 2) yield()           // heartbeat under fence 1, answered later
        val second = async { c.claim() }; while (gates.size < 3) yield(); gates[2].complete(ok(2)); assertEquals(2L, second.await().generation)
        gates[1].complete(conflict("fenced", "generation" to 9L, "active" to true, "device_id" to "网页播放器 1A2B"))   // owner finding 1a
        assertEquals("stale_response", oldBeat.await().reason)
        assertEquals(Status.WRITING, c.status); assertEquals(2L, c.generation)
        val oldRelease = async { c.release() }; while (gates.size < 4) yield()         // release under fence 2, answered later
        val third = async { c.claim() }; while (gates.size < 5) yield(); gates[4].complete(ok(3)); assertEquals(3L, third.await().generation)
        gates[3].complete(applied(1))                                                   // owner finding 1b
        assertEquals("stale_response", oldRelease.await().reason)
        assertEquals(Status.WRITING, c.status); assertEquals(3L, c.generation)
        val slow = async { c.claim() }; while (gates.size < 6) yield()
        val fast = async { c.claim() }; while (gates.size < 7) yield()
        gates[6].complete(ok(5)); assertEquals(5L, fast.await().generation)
        gates[5].complete(ok(4)); assertEquals("superseded", slow.await().reason)
        assertEquals(5L, c.generation)
    }

    @Test fun disposalMakesEveryPendingAnswerInert() = runBlocking {
        val gates = mutableListOf<CompletableDeferred<Response>>()
        val fake = Fake { _, _, _ -> gate().also { gates += it }.await() }
        val c = client(fake)
        val first = async { c.claim() }; while (gates.size < 1) yield(); gates[0].complete(ok(1)); first.await()
        val p = async { c.progress(3.0, 9.0, true) }; while (gates.size < 2) yield()
        c.dispose()
        gates[1].complete(applied(1))
        assertEquals("stale_response", p.await().reason)
        assertEquals(Status.DISPOSED, c.status)
        assertEquals("disposed", c.heartbeat().reason)
    }

    @Test fun progressHeartbeatAndReleaseShareOneSerializedSequenceAndOutOfOrderIsNotASave() = runBlocking {
        val gates = mutableListOf<CompletableDeferred<Response>>()
        val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(3) else gate().also { gates += it }.await() }
        val c = client(fake)
        c.claim()
        val a = async { c.progress(10.0, 100.0, true) }
        val b = async { c.heartbeat() }
        val d = async { c.progress(20.0, 100.0, false) }
        while (gates.size < 1) yield()
        assertEquals(1, gates.size)                                                     // serialized: the next packet waits
        gates[0].complete(applied(1)); assertEquals(1L, a.await().sequence)
        while (gates.size < 2) yield()
        gates[1].complete(Response(409, mapOf("ok" to false, "ignored" to true, "reason" to "out_of_order")))
        val dup = b.await(); assertFalse(dup.ok); assertTrue(dup.ignored); assertEquals("out_of_order", dup.reason); assertEquals(Status.WRITING, c.status)
        while (gates.size < 3) yield()
        gates[2].complete(applied(3)); assertEquals(3L, d.await().sequence)
        val rel = async { c.release(keepalive = true) }; while (gates.size < 4) yield()
        gates[3].complete(applied(4)); assertTrue(rel.await().ok)
        assertEquals(Status.RELEASED, c.status)
        val packets = fake.calls.drop(1)
        assertEquals(listOf(1L, 2L, 3L, 4L), packets.map { it.body["sequence"] })
        assertTrue(packets.all { it.body["generation"] == 3L && it.body["session"] == session && it.body["writer_token"] == fake.calls[0].body["writer_token"] })
        assertTrue(packets[3].keepalive)
    }

    @Test fun fencedExpiredAndSessionRequiredStopWritingAndNeedAnExplicitReclaim() = runBlocking {
        for (reason in listOf("fenced", "expired", "session_required")) {
            var n = 0L
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(++n) else conflict(reason, "generation" to 7L, "active" to true, "device_id" to "<script>TV</script>") }
            val c = client(fake)
            c.claim()
            val r = c.progress(1.0, 2.0, true)
            assertFalse(r.ok); assertEquals(reason, r.reason)
            assertEquals(if (reason == "expired") Status.EXPIRED else Status.FENCED, c.status); assertTrue(c.unsaved)
            assertEquals("<script>TV</script>", c.conflict?.device)                   // plain text for the UI
            assertEquals(c.status.name.lowercase(), c.heartbeat().reason)             // no background retries
            assertEquals(2, fake.calls.size)
            c.claim(Mode.TAKEOVER, 7)
            assertEquals(7L, fake.calls[2].body["expected_generation"])
            c.progress(1.0, 2.0, true)
            assertEquals(1L, fake.calls[3].body["sequence"])                            // a new fence restarts the sequence
        }
    }

    @Test fun aRetriedClaimIsVerifiedThroughStateBeforeItCountsAsOwnership() = runBlocking {
        val good = Fake { _, _, n -> if (n == 1) Response(0, null) else ok(2) }
        good.stateAnswer = Response(200, mapOf("media_key" to "media-1", "generation" to 2L, "active" to true, "expires" to 9.0, "device_id" to "电视播放器 7F3A"))
        val c = client(good)
        assertTrue(c.claim().ok)
        assertEquals(good.calls[0].body["request_id"], good.calls[1].body["request_id"])   // the SAME packet was retried
        assertTrue(good.calls[2].path.startsWith(PlaybackWriterClient.STATE))
        val stale = Fake { _, _, n -> if (n == 1) Response(0, null) else ok(2) }
        stale.stateAnswer = Response(200, mapOf("media_key" to "media-1", "generation" to 3L, "active" to true, "expires" to 9.0, "device_id" to "网页播放器 1A2B"))
        val c2 = client(stale)
        val r = c2.claim()
        assertFalse(r.ok); assertEquals("verify_failed", r.reason); assertEquals(Status.STALE, c2.status); assertEquals("网页播放器 1A2B", c2.conflict?.device)
        assertEquals("stale", c2.progress(0.0, 1.0, false).reason)
    }

    @Test fun disabledAfterV2WasChosenIsAVisibleFailureNeverALegacyFallback() = runBlocking {
        val answers = ArrayDeque(listOf(ok(1), Response(503, mapOf("error" to "playback writer disabled", "reason" to "disabled"))))
        val fake = Fake { _, _, _ -> answers.removeFirst() }
        val c = client(fake)
        c.claim()
        val r = c.progress(0.0, 1.0, true)
        assertFalse(r.ok); assertEquals("disabled", r.reason); assertTrue(c.unsaved); assertEquals("disabled", c.lastError)
        val off = Fake { _, _, _ -> throw AssertionError("no claim expected") }
        off.stateAnswer = Response(503, mapOf("reason" to "disabled"))
        val c2 = client(off)
        assertFalse(c2.state().supported); assertEquals(Status.UNSUPPORTED, c2.status)
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
