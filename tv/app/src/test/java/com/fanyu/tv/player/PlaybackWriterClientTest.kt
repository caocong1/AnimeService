package com.fanyu.tv.player

import com.fanyu.tv.player.PlaybackWriterClient.Mode
import com.fanyu.tv.player.PlaybackWriterClient.Response
import com.fanyu.tv.player.PlaybackWriterClient.Status
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.*
import org.junit.Test

class PlaybackWriterClientTest {
    private class Call(val path: String, val body: Map<String, Any?>, val keepalive: Boolean)

    /** Scripted transport: [answer] decides each reply (it may suspend or throw), every request is recorded. */
    private class Fake(val answer: suspend (String, Map<String, Any?>, Int) -> Response) : PlaybackWriterClient.Transport {
        val calls = mutableListOf<Call>()
        var stateAnswer = state(0)
        override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response {
            calls += Call(path, body, keepalive)
            return answer(path, body, calls.size)
        }
        override suspend fun get(path: String): Response { calls += Call(path, emptyMap(), false); return stateAnswer }
    }

    private class Gates {
        val list = mutableListOf<CompletableDeferred<Response>>()
        suspend fun next(): Response = CompletableDeferred<Response>().also { list += it }.await()
        suspend fun until(n: Int) { while (list.size < n) yield() }
    }

    private var seed = 0
    private val key = "0123456789abcdef0123456789abcdef"
    private val session = "session-aaaaaaaaaaaaaaaa"
    private fun client(fake: Fake, retries: Int = 1) =
        PlaybackWriterClient(key, session, "电视播放器 7F3A", fake, { n -> "r" + (++seed).toString().padStart(3, '0') + "x".repeat(n) }, retries)
    private fun ok(generation: Long, expires: Double = 999.0, vararg extra: Pair<String, Any?>) =
        Response(200, mapOf("ok" to true, "media_key" to key, "requested_media_key" to key, "generation" to generation, "expires" to expires) + extra)
    private fun applied(generation: Long, sequence: Long) = Response(200, mapOf("ok" to true, "generation" to generation, "sequence" to sequence))
    private fun conflict(reason: String, vararg extra: Pair<String, Any?>) =
        Response(409, mapOf("error" to "conflict", "ok" to false, "reason" to reason, "media_key" to key, "requested_media_key" to key) + extra)
    companion object {
        fun state(generation: Long, active: Boolean = false, device: String? = null) =
            Response(200, mapOf("media_key" to "0123456789abcdef0123456789abcdef", "requested_media_key" to "0123456789abcdef0123456789abcdef",
                "generation" to generation, "active" to active, "expires" to 0.0, "device_id" to device))
    }

    @Test fun negotiationLegacyFenceAndLabels() {
        assertTrue(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to true))))
        assertFalse(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to "true"))))
        assertFalse(PlaybackWriterClient.negotiated(null))
        assertTrue(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "session_required")))
        assertTrue(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "fenced")))
        assertFalse(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "other"))); assertFalse(PlaybackWriterClient.legacyFenced(500, mapOf("reason" to "session_required")))
        assertEquals("电视播放器 <b>X</b>", PlaybackWriterClient.sanitizeLabel(" 电视播放器 <b>X</b>\u0007​ "))
        assertEquals(32, PlaybackWriterClient.sanitizeLabel("x".repeat(40))!!.length)
        assertNull(PlaybackWriterClient.sanitizeLabel("\u0000‎"))
        assertEquals("电视播放器 ABAB", PlaybackWriterClient.deviceLabel("电视播放器") { n -> "ab".repeat(n) })
    }

    @Test fun weakIdentitiesAreRefused() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> ok(1) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient(key, "short", "电视播放器", fake) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient(key, session, "\u0000", fake) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient("", session, "电视播放器", fake) }
        val weak = PlaybackWriterClient(key, session, "电视播放器", fake, { "ab" })
        assertThrows(IllegalArgumentException::class.java) { runBlocking { weak.claim() } }
        assertEquals(0, fake.calls.size)
    }

    @Test fun stateNeverClaimsRemembersTheGenerationAndRequiresTheEcho() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> ok(8) }
        fake.stateAnswer = state(7, true, "网页播放器 1A2B")
        val c = client(fake)
        val s = c.state()
        assertTrue(s.supported); assertEquals(7L, s.generation); assertTrue(s.active); assertEquals("网页播放器 1A2B", s.device); assertEquals(Status.IDLE, c.status)
        c.claim()
        assertEquals(7L, fake.calls[1].body["expected_generation"])
        val wrong = Fake { _, _, _ -> ok(1) }
        wrong.stateAnswer = Response(200, mapOf("media_key" to key, "requested_media_key" to "other", "generation" to 1L, "active" to false))
        val c2 = client(wrong)
        assertEquals("invalid_state", c2.state().reason); assertEquals(Status.HALTED, c2.status)
    }

    @Test fun aClaimNeedsOkCanonicalKeyEchoGenerationAndFiniteExpires() = runBlocking<Unit> {
        val answers = ArrayDeque(listOf(Response(500, null), Response(200, mapOf("ok" to true)),
            Response(200, mapOf("ok" to true, "media_key" to "other", "requested_media_key" to key, "generation" to 1L, "expires" to 1.0)),
            Response(200, mapOf("ok" to true, "media_key" to key, "requested_media_key" to "other", "generation" to 1L, "expires" to 1.0)),
            ok(1, Double.POSITIVE_INFINITY), ok(1)))
        val fake = Fake { _, _, _ -> answers.removeFirst() }
        val c = client(fake, retries = 0)
        assertEquals("server_500", c.claim().reason); assertEquals(Status.ERROR, c.status)
        repeat(4) { assertEquals("protocol_200", c.claim().reason); assertEquals(Status.HALTED, c.status) }
        assertTrue(c.claim().ok); assertEquals(Status.WRITING, c.status)
    }

    @Test fun aSaveSucceedsOnlyWithItsOwnGenerationAndSequence() = runBlocking<Unit> {
        suspend fun after(reply: Response): Pair<PlaybackWriterClient, PlaybackWriterClient.SendResult> {
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(3) else reply }
            val c = client(fake); c.claim(); return c to c.progress(1.0, 9.0, true)
        }
        after(Response(200, mapOf("ok" to true))).let { (c, r) -> assertEquals("protocol_200", r.reason); assertEquals(Status.HALTED, c.status) }   // owner repro 2
        after(applied(2, 1)).let { (c, r) -> assertEquals("protocol_200", r.reason); assertEquals(Status.HALTED, c.status) }
        after(applied(3, 9)).let { (c, r) -> assertEquals("protocol_200", r.reason) }
        after(applied(3, 1)).let { (c, r) -> assertTrue(r.ok); assertEquals(1L, r.sequence); assertFalse(c.unsaved) }
        after(Response(404, mapOf("detail" to "Not Found"))).let { (c, r) -> assertEquals("protocol_404", r.reason); assertEquals(Status.HALTED, c.status); assertTrue(c.unsaved) }
        after(Response(500, null)).let { (c, r) -> assertEquals("server_500", r.reason); assertEquals(Status.WRITING, c.status); assertTrue(c.unsaved) }   // a server fault keeps the fence
    }

    @Test fun conflictTakeoverAndNoDoubleClaim() = runBlocking<Unit> {
        val fake = Fake { _, _, n -> if (n == 1) conflict("takeover_required", "generation" to 4L, "active" to true, "device_id" to "网页播放器 1A2B") else ok(5, 50.0) }
        val c = client(fake)
        val first = c.claim()
        assertFalse(first.ok); assertEquals("takeover_required", first.reason); assertEquals(4L, first.generation); assertEquals("网页播放器 1A2B", first.device); assertEquals(Status.CONFLICT, c.status)
        assertThrows(IllegalArgumentException::class.java) { runBlocking { c.claim(Mode.TAKEOVER) } }
        val second = c.claim(Mode.TAKEOVER, 4)
        assertTrue(second.ok); assertEquals(5L, second.generation); assertEquals(Status.WRITING, c.status)
        assertEquals("takeover", fake.calls[1].body["mode"]); assertEquals(4L, fake.calls[1].body["expected_generation"])
        assertNotEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"]); assertNotEquals(fake.calls[0].body["writer_token"], fake.calls[1].body["writer_token"])
    }

    @Test fun queuedPacketsFromAnOlderFenceAreDroppedNeverReboundToANewClaim() = runBlocking<Unit> {   // owner repro 1
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); assertEquals(1L, first.await().generation)
        val p10 = async { c.progress(10.0, 100.0, true) }; g.until(2)                    // in flight under g1 (seq 1)
        val p11 = async { c.progress(11.0, 100.0, true) }; yield()                       // queued behind p10
        val second = async { c.claim() }; g.until(3); g.list[2].complete(ok(2)); assertEquals(2L, second.await().generation)
        g.list[1].complete(applied(1, 1))                                                 // the old in-flight answer arrives late
        assertEquals("stale_response", p10.await().reason)
        assertEquals("stale_response", p11.await().reason)
        assertEquals(3, g.list.size)                                                      // p11 was never sent
        val fresh = async { c.progress(12.0, 100.0, true) }; g.until(4)
        assertEquals(2L, fake.calls[3].body["generation"]); assertEquals(1L, fake.calls[3].body["sequence"])
        g.list[3].complete(applied(2, 1)); assertTrue(fresh.await().ok)
    }

    @Test fun lateAnswersOfOlderHeartbeatReleaseOrClaimNeverTouchTheCurrentFence() = runBlocking<Unit> {
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); first.await()
        val oldBeat = async { c.heartbeat() }; g.until(2)
        val second = async { c.claim() }; g.until(3); g.list[2].complete(ok(2)); assertEquals(2L, second.await().generation)
        g.list[1].complete(conflict("fenced", "generation" to 9L, "active" to true, "device_id" to "网页播放器 1A2B"))
        assertEquals("stale_response", oldBeat.await().reason); assertEquals(Status.WRITING, c.status); assertEquals(2L, c.generation)
        val oldRelease = async { c.release() }; g.until(4)
        val third = async { c.claim() }; g.until(5); g.list[4].complete(ok(3)); assertEquals(3L, third.await().generation)
        g.list[3].complete(applied(2, 1))
        assertEquals("stale_response", oldRelease.await().reason); assertEquals(Status.WRITING, c.status); assertEquals(3L, c.generation)
        val slow = async { c.claim() }; g.until(6)
        val fast = async { c.claim() }; yield()
        assertEquals("claim_in_progress", fast.await().reason)                            // one claim at a time
        g.list[5].complete(ok(4)); assertEquals(4L, slow.await().generation)
    }

    @Test fun disabledNotFoundOrAuthorizationFailuresHaltWriting() = runBlocking<Unit> {   // owner repro 3
        for ((status, body, expected) in listOf(Triple(503, mapOf("reason" to "disabled"), "disabled_503"), Triple(404, mapOf("detail" to "x"), "protocol_404"), Triple(401, mapOf("error" to "login"), "unauthorized_401"))) {
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(1) else Response(status, body) }
            val c = client(fake)
            c.claim()
            assertEquals(expected, c.progress(0.0, 1.0, true).reason); assertEquals(Status.HALTED, c.status); assertTrue(c.unsaved)
            assertEquals("halted", c.heartbeat().reason); assertEquals("halted", c.progress(5.0, 9.0, false).reason)
            assertEquals(2, fake.calls.size)
        }
    }

    @Test fun disposedIsTerminalExceptForAFinalSnapshotCapturedBefore() = runBlocking<Unit> {   // owner repro 4 + TV repro
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); first.await()
        val seen = mutableListOf<Status>(); c.onChange { seen += it }
        val inflight = async { c.progress(3.0, 9.0, true) }; g.until(2)
        val queued = async { c.progress(4.0, 9.0, true) }; yield()
        val final = c.final(5.0, 9.0, false)                                              // captured now, lane moves
        assertEquals(Status.SUSPENDED, c.status)
        c.dispose()
        val finalJob = async { final() }; g.until(3)                                       // the final packet still leaves after dispose
        assertEquals(5.0, fake.calls[2].body["position"]); assertEquals(2L, fake.calls[2].body["sequence"]); assertTrue(fake.calls[2].keepalive)
        g.list[1].complete(applied(1, 1)); g.list[2].complete(applied(1, 2))
        assertEquals("stale_response", inflight.await().reason); assertEquals("stale_response", queued.await().reason)
        assertTrue(finalJob.await().ok)
        assertEquals("disposed", c.claim().reason); assertEquals("disposed", c.state().reason); assertEquals("disposed", c.heartbeat().reason)
        assertEquals(3, g.list.size); assertEquals(Status.DISPOSED, c.status)
        assertFalse(seen.contains(Status.DISPOSED))                                       // the answer updated no listener
    }

    @Test fun transportExceptionsAreNetworkFailuresAndCancellationIsRethrown() = runBlocking<Unit> {   // owner repro 5
        var n = 0
        val fake = Fake { _, _, _ -> n++; if (n == 1) throw IllegalStateException("boom") else if (n == 2) ok(1) else throw IllegalStateException("boom") }
        fake.stateAnswer = state(1, true, "电视播放器 7F3A")
        val c = client(fake)
        assertTrue(c.claim().ok)                                                            // the same packet retried, then verified
        assertEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"]); assertTrue(fake.calls[2].path.startsWith(PlaybackWriterClient.STATE))
        assertEquals("network", c.progress(1.0, 2.0, true).reason); assertEquals(Status.WRITING, c.status); assertTrue(c.unsaved)
        val cancelling = Fake { _, _, _ -> throw CancellationException("cancelled") }
        val c2 = client(cancelling)
        assertThrows(CancellationException::class.java) { runBlocking { c2.claim() } }
    }

    @Test fun serverChangeHaltsInsteadOfReplayingElsewhere() = runBlocking<Unit> {
        val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(1) else Response(PlaybackWriterClient.SERVER_CHANGED, null) }
        val c = client(fake)
        c.claim()
        assertEquals("protocol", c.progress(1.0, 2.0, true).reason); assertEquals(Status.HALTED, c.status)
        assertEquals(2, fake.calls.size)
    }

    @Test fun progressHeartbeatAndReleaseShareOneSerializedSequenceAndOutOfOrderIsNotASave() = runBlocking<Unit> {
        val g = Gates()
        val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(3) else g.next() }
        val c = client(fake)
        c.claim()
        val a = async { c.progress(10.0, 100.0, true) }; val b = async { c.heartbeat() }; val d = async { c.progress(20.0, 100.0, false) }
        g.until(1); yield(); assertEquals(1, g.list.size)
        g.list[0].complete(applied(3, 1)); assertEquals(1L, a.await().sequence); g.until(2)
        g.list[1].complete(Response(409, mapOf("ok" to false, "ignored" to true, "reason" to "out_of_order")))
        val dup = b.await(); assertFalse(dup.ok); assertTrue(dup.ignored); assertTrue(c.unsaved); assertEquals(Status.WRITING, c.status); g.until(3)
        g.list[2].complete(applied(3, 3)); assertEquals(3L, d.await().sequence); assertFalse(c.unsaved)
        val rel = async { c.release() }; g.until(4); g.list[3].complete(applied(3, 4)); assertTrue(rel.await().ok); assertEquals(Status.RELEASED, c.status)
        val packets = fake.calls.drop(1)
        assertEquals(listOf(1L, 2L, 3L, 4L), packets.map { it.body["sequence"] })
        assertTrue(packets.all { it.body["generation"] == 3L && it.body["writer_token"] == fake.calls[0].body["writer_token"] })
    }

    @Test fun fencedExpiredAndSessionRequiredStopWritingUntilAnExplicitReclaim() = runBlocking<Unit> {
        for (reason in listOf("fenced", "expired", "session_required")) {
            var n = 0L
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(++n) else conflict(reason, "generation" to 7L, "active" to true, "device_id" to "<script>TV</script>") }
            val c = client(fake)
            c.claim()
            assertEquals(reason, c.progress(1.0, 2.0, true).reason)
            assertEquals(if (reason == "expired") Status.EXPIRED else Status.FENCED, c.status); assertEquals("<script>TV</script>", c.conflict?.device)
            assertEquals(c.status.name.lowercase(), c.heartbeat().reason); assertEquals(2, fake.calls.size)
            c.claim(Mode.TAKEOVER, 7); assertEquals(7L, fake.calls[2].body["expected_generation"])
            c.progress(1.0, 2.0, true); assertEquals(1L, fake.calls[3].body["sequence"])
        }
    }

    @Test fun retriedOrReplayedClaimsAreVerifiedThroughState() = runBlocking<Unit> {
        val good = Fake { _, _, n -> if (n == 1) Response(0, null) else ok(2) }
        good.stateAnswer = state(2, true, "电视播放器 7F3A")
        assertTrue(client(good).claim().ok)
        val replay = Fake { _, _, _ -> ok(2, 999.0, "replayed" to true) }
        replay.stateAnswer = state(3, true, "网页播放器 1A2B")
        val c = client(replay)
        val r = c.claim()
        assertFalse(r.ok); assertEquals("verify_failed", r.reason); assertEquals(Status.STALE, c.status); assertEquals("网页播放器 1A2B", c.conflict?.device)
    }

    @Test fun anOlderServerLeavesTheClientUnsupportedBeforeAnyClaim() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> throw AssertionError("must not be called") }
        fake.stateAnswer = Response(503, mapOf("reason" to "disabled"))
        val c = client(fake)
        assertFalse(c.state().supported); assertEquals(Status.UNSUPPORTED, c.status); assertEquals(1, fake.calls.size)
    }
}
