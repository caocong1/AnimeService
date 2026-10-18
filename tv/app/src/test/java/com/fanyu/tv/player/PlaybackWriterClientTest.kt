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
        var stateAnswer: suspend () -> Response = { state(0) }
        override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): Response {
            calls += Call(path, body, keepalive)
            return answer(path, body, calls.size)
        }
        override suspend fun get(path: String): Response { calls += Call(path, emptyMap(), false); return stateAnswer() }
    }

    private class Gates {
        val list = mutableListOf<CompletableDeferred<Response>>()
        suspend fun next(): Response = CompletableDeferred<Response>().also { list += it }.await()
        suspend fun until(n: Int) { while (list.size < n) yield() }
    }

    private var seed = 0
    private val session = "session-aaaaaaaaaaaaaaaa"
    private fun client(fake: Fake, retries: Int = 1) =
        PlaybackWriterClient(KEY, session, "电视播放器 7F3A", fake, { n -> "r" + (++seed).toString().padStart(3, '0') + "x".repeat(n) }, retries)
    private fun ok(generation: Long, expires: Double = 999.0, vararg extra: Pair<String, Any?>) =
        Response(200, mapOf("ok" to true, "media_key" to KEY, "requested_media_key" to KEY, "generation" to generation, "expires" to expires) + extra)
    private fun applied(generation: Long, sequence: Long) = Response(200, mapOf("ok" to true, "generation" to generation, "sequence" to sequence))
    private fun conflict(reason: String, generation: Long = 7L, device: String? = "网页播放器 1A2B", vararg extra: Pair<String, Any?>) =
        Response(409, mapOf("error" to "conflict", "ok" to false, "reason" to reason, "media_key" to KEY, "requested_media_key" to KEY,
            "generation" to generation, "active" to true, "expires" to 5.0, "device_id" to device) + extra)

    companion object {
        const val KEY = "0123456789abcdef0123456789abcdef"
        fun state(generation: Long, active: Boolean = false, device: String? = null) =
            Response(200, mapOf("media_key" to KEY, "requested_media_key" to KEY, "generation" to generation, "active" to active, "expires" to 0.0, "device_id" to device))
    }

    @Test fun negotiationLegacyFenceLabelsAndSafeIntegers() {
        assertTrue(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to true))))
        assertFalse(PlaybackWriterClient.negotiated(mapOf("playback_writer" to mapOf("protocol" to "playback-writer/v2", "enabled" to "true"))))
        assertFalse(PlaybackWriterClient.negotiated(null))
        assertTrue(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "session_required"))); assertTrue(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "fenced")))
        assertFalse(PlaybackWriterClient.legacyFenced(409, mapOf("reason" to "other"))); assertFalse(PlaybackWriterClient.legacyFenced(500, mapOf("reason" to "session_required")))
        assertEquals("电视播放器 <b>X</b>", PlaybackWriterClient.sanitizeLabel(" 电视播放器 <b>X</b>\u0007​ "))
        assertEquals(32, PlaybackWriterClient.sanitizeLabel("x".repeat(40))!!.length); assertNull(PlaybackWriterClient.sanitizeLabel("\u0000‎"))
        assertEquals("电视播放器 ABAB", PlaybackWriterClient.deviceLabel("电视播放器") { n -> "ab".repeat(n) })
        assertEquals(5L, PlaybackWriterClient.safeLong(5)); assertEquals(5L, PlaybackWriterClient.safeLong(5.0))
        assertNull(PlaybackWriterClient.safeLong(-1)); assertNull(PlaybackWriterClient.safeLong(Long.MAX_VALUE)); assertNull(PlaybackWriterClient.safeLong(9007199254740992.0)); assertNull(PlaybackWriterClient.safeLong(1.5))
    }

    @Test fun weakIdentitiesAreRefused() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> ok(1) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient(KEY, "short", "电视播放器", fake) }
        assertThrows(IllegalArgumentException::class.java) { PlaybackWriterClient(KEY, session, "\u0000", fake) }
        val weak = PlaybackWriterClient(KEY, session, "电视播放器", fake, { "ab" })
        assertThrows(IllegalArgumentException::class.java) { runBlocking { weak.claim() } }
        assertEquals(0, fake.calls.size)
    }

    @Test fun stateNeverClaimsRemembersTheGenerationAndRequiresTheExactEcho() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> ok(8) }
        fake.stateAnswer = { state(7, true, "网页播放器 1A2B") }
        val c = client(fake)
        val s = c.state()
        assertTrue(s.supported); assertEquals(7L, s.generation); assertTrue(s.active); assertEquals("网页播放器 1A2B", s.device); assertEquals(Status.IDLE, c.status)
        c.claim(); assertEquals(7L, fake.calls[1].body["expected_generation"])
        val missing = Fake { _, _, _ -> ok(1) }
        missing.stateAnswer = { Response(200, mapOf("media_key" to KEY, "generation" to 1L, "active" to false)) }   // no requested_media_key
        val c2 = client(missing)
        assertEquals("invalid_state", c2.state().reason); assertEquals(Status.HALTED, c2.status)
        assertEquals("halted", c2.claim().reason); assertEquals(1, missing.calls.size)                      // a halted instance never claims
    }

    @Test fun aClaimNeedsOkExactEchoGenerationAndFiniteExpiresAndConflictsNeedCanonicalFields() = runBlocking<Unit> {
        val answers = ArrayDeque(listOf(Response(500, null), ok(1, 999.0, "requested_media_key" to null),
            Response(200, mapOf("ok" to true, "media_key" to "other", "requested_media_key" to KEY, "generation" to 1L, "expires" to 1.0)),
            Response(409, mapOf("ok" to false, "reason" to "takeover_required")), ok(1, Double.POSITIVE_INFINITY), ok(1)))
        val fake = Fake { _, _, _ -> answers.removeFirst() }
        val c = client(fake, retries = 0)
        assertEquals("server_500", c.claim().reason); assertEquals(Status.ERROR, c.status)
        assertEquals("protocol_200", c.claim().reason); assertEquals(Status.HALTED, c.status)          // missing echo: owner finding 3
        val fresh = client(Fake { _, _, _ -> answers.removeFirst() }, retries = 0)
        assertEquals("protocol_200", fresh.claim().reason)                                               // wrong canonical
        val fresh2 = client(Fake { _, _, _ -> answers.removeFirst() }, retries = 0)
        assertEquals("protocol_409", fresh2.claim().reason); assertEquals(Status.HALTED, fresh2.status)   // a conflict without canonical fields is not a conflict
        val fresh3 = client(Fake { _, _, _ -> answers.removeFirst() }, retries = 0)
        assertEquals("protocol_200", fresh3.claim().reason)                                              // infinite expires
        val fresh4 = client(Fake { _, _, _ -> answers.removeFirst() }, retries = 0)
        assertTrue(fresh4.claim().ok); assertEquals(Status.WRITING, fresh4.status)
    }

    @Test fun aSaveSucceedsOnlyWithItsOwnGenerationAndSequence() = runBlocking<Unit> {
        suspend fun after(reply: Response): Pair<PlaybackWriterClient, PlaybackWriterClient.SendResult> {
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(3) else reply }
            val c = client(fake); c.claim(); return c to c.progress(1.0, 9.0, true)
        }
        after(Response(200, mapOf("ok" to true))).let { (c, r) -> assertEquals("protocol_200", r.reason); assertEquals(Status.HALTED, c.status) }
        after(applied(2, 1)).let { (c, r) -> assertEquals("protocol_200", r.reason); assertEquals(Status.HALTED, c.status) }
        after(applied(3, 9)).let { (_, r) -> assertEquals("protocol_200", r.reason) }
        after(Response(200, mapOf("ok" to true, "ignored" to true, "generation" to 3L, "sequence" to 1L))).let { (c, r) -> assertFalse(r.ok); assertTrue(r.ignored); assertTrue(c.unsaved) }
        after(applied(3, 1)).let { (c, r) -> assertTrue(r.ok); assertEquals(1L, r.sequence); assertFalse(c.unsaved) }
        after(Response(404, mapOf("detail" to "x"))).let { (c, r) -> assertEquals("protocol_404", r.reason); assertEquals(Status.HALTED, c.status) }
        after(Response(500, null)).let { (c, r) -> assertEquals("server_500", r.reason); assertEquals(Status.WRITING, c.status); assertTrue(c.unsaved) }
    }

    @Test fun conflictTakeoverAndNoDoubleClaim() = runBlocking<Unit> {
        val fake = Fake { _, _, n -> if (n == 1) conflict("takeover_required", 4L) else ok(5, 50.0) }
        val c = client(fake)
        val first = c.claim()
        assertFalse(first.ok); assertEquals("takeover_required", first.reason); assertEquals(4L, first.generation); assertEquals("网页播放器 1A2B", first.device); assertEquals(Status.CONFLICT, c.status)
        assertThrows(IllegalArgumentException::class.java) { runBlocking { c.claim(Mode.TAKEOVER) } }
        val second = c.claim(Mode.TAKEOVER, 4)
        assertTrue(second.ok); assertEquals(5L, second.generation); assertEquals(Status.WRITING, c.status)
        assertEquals("takeover", fake.calls[1].body["mode"]); assertEquals(4L, fake.calls[1].body["expected_generation"])
        assertNotEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"]); assertNotEquals(fake.calls[0].body["writer_token"], fake.calls[1].body["writer_token"])
    }

    @Test fun queuedPacketsFromAnOlderFenceAreDroppedNeverReboundToANewClaim() = runBlocking<Unit> {
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); assertEquals(1L, first.await().generation)
        val p10 = async { c.progress(10.0, 100.0, true) }; g.until(2)
        val p11 = async { c.progress(11.0, 100.0, true) }; yield()
        val second = async { c.claim() }; g.until(3); g.list[2].complete(ok(2)); assertEquals(2L, second.await().generation)
        g.list[1].complete(applied(1, 1))
        assertEquals("stale_response", p10.await().reason); assertEquals("stale_response", p11.await().reason)
        assertEquals(3, g.list.size)
        val fresh = async { c.progress(12.0, 100.0, true) }; g.until(4)
        assertEquals(2L, fake.calls[3].body["generation"]); assertEquals(1L, fake.calls[3].body["sequence"])
        g.list[3].complete(applied(2, 1)); assertTrue(fresh.await().ok)
    }

    @Test fun repliesArrivingAfterTheFinalSnapshotAreInert() = runBlocking<Unit> {   // owner round-20 finding 1
        for (late in listOf(Response(500, null), conflict("fenced"), applied(1, 1), Response(200, mapOf("ok" to true)))) {
            val g = Gates()
            val fake = Fake { _, _, _ -> g.next() }
            fake.stateAnswer = { g.next() }
            val c = client(fake)
            val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); first.await()
            val p10 = async { c.progress(10.0, 100.0, true) }; g.until(2)                    // in flight under the fence
            val s = async { c.state() }; g.until(3)                                           // an old state read, answered later
            val finalJob = c.final(30.0, 100.0, false)
            assertEquals(Status.SUSPENDED, c.status)
            g.list[1].complete(late)
            assertEquals("stale_response", p10.await().reason)
            assertEquals(Status.SUSPENDED, c.status); assertFalse(c.unsaved)
            g.list[2].complete(state(9, true, "网页播放器 1A2B"))
            assertEquals("stale_response", s.await().reason); assertEquals(Status.SUSPENDED, c.status)
            assertEquals("suspended", c.progress(40.0, 100.0, true).reason)                   // nothing more is written
            val sent = async { finalJob() }; g.until(4)
            assertEquals(30.0, fake.calls[3].body["position"]); assertEquals(2L, fake.calls[3].body["sequence"])
            g.list[3].complete(applied(1, 2)); assertTrue(sent.await().ok)
            assertEquals(Status.SUSPENDED, c.status)                                          // the final answer changes no state
            assertEquals(4, g.list.size)
        }
    }

    @Test fun theFinalPermitIsConsumedOnceAndVerifiedStrictly() = runBlocking<Unit> {   // owner round-20 finding 2
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); first.await()
        val seen = mutableListOf<Status>(); c.onChange { seen += it }
        val job = c.final(5.0, 9.0, false)
        c.dispose()
        val a = async { job() }; g.until(2)
        val b = async { job() }; yield()
        assertEquals("stale_response", b.await().reason); assertEquals(2, g.list.size)        // the second call sends nothing
        g.list[1].complete(Response(200, mapOf("ok" to true, "ignored" to true, "generation" to 999L, "sequence" to 1L)))
        assertFalse(a.await().ok)                                                             // wrong generation / ignored is not a save
        assertTrue(seen.none { it == Status.DISPOSED })
        assertEquals(Status.DISPOSED, c.status)
        val g2 = Gates(); val fake2 = Fake { _, _, _ -> g2.next() }; val c2 = client(fake2)
        val f2 = async { c2.claim() }; g2.until(1); g2.list[0].complete(ok(1)); f2.await()
        val job2 = c2.final(5.0, 9.0, false)
        val reclaim = async { c2.claim() }; g2.until(2); g2.list[1].complete(ok(2)); reclaim.await()   // a new claim invalidates the old permit
        assertEquals("stale_response", job2().reason); assertEquals(2, g2.list.size)
        assertEquals(Status.WRITING, c2.status)
    }

    @Test fun disabledNotFoundAuthorizationOrServerChangeHaltAndRefuseNewClaims() = runBlocking<Unit> {   // owner findings 3, 4
        for ((status, body, expected) in listOf(Triple(503, mapOf("reason" to "disabled"), "disabled_503"), Triple(404, mapOf("detail" to "x"), "protocol_404"),
                                                Triple(401, mapOf("error" to "login"), "unauthorized_401"), Triple(PlaybackWriterClient.SERVER_CHANGED, null, "protocol"))) {
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(1) else Response(status, body) }
            val c = client(fake)
            c.claim()
            assertEquals(expected, c.progress(0.0, 1.0, true).reason); assertEquals(Status.HALTED, c.status); assertTrue(c.unsaved)
            assertEquals("halted", c.heartbeat().reason); assertEquals("halted", c.claim().reason); assertEquals("halted", c.claim(Mode.TAKEOVER, 1).reason)
            assertEquals(2, fake.calls.size)
        }
        val replay = Fake { _, _, _ -> ok(2, 999.0, "replayed" to true) }
        replay.stateAnswer = { Response(503, mapOf("reason" to "disabled")) }
        val c = client(replay)
        assertEquals("halted", c.claim().reason); assertEquals(Status.HALTED, c.status)      // a disabled state check never becomes STALE
        assertEquals("halted", c.claim().reason); assertEquals(2, replay.calls.size)
    }

    @Test fun disposedIsTerminalForEverythingButTheFinalPermit() = runBlocking<Unit> {
        val g = Gates()
        val fake = Fake { _, _, _ -> g.next() }
        val c = client(fake)
        val first = async { c.claim() }; g.until(1); g.list[0].complete(ok(1)); first.await()
        val inflight = async { c.progress(3.0, 9.0, true) }; g.until(2)
        val queued = async { c.progress(4.0, 9.0, true) }; yield()
        c.dispose()
        g.list[1].complete(applied(1, 1))
        assertEquals("stale_response", inflight.await().reason); assertEquals("stale_response", queued.await().reason)
        assertEquals("disposed", c.claim().reason); assertEquals("disposed", c.state().reason); assertEquals("disposed", c.heartbeat().reason)
        assertEquals("disposed", c.final(1.0, 2.0, false)().reason)                            // no permit can be minted after dispose
        assertEquals(2, g.list.size); assertEquals(Status.DISPOSED, c.status)
    }

    @Test fun transportExceptionsAreNetworkFailuresAndCancellationIsRethrown() = runBlocking<Unit> {
        var n = 0
        val fake = Fake { _, _, _ -> n++; if (n == 1) throw IllegalStateException("boom") else if (n == 2) ok(1) else throw IllegalStateException("boom") }
        fake.stateAnswer = { state(1, true, "电视播放器 7F3A") }
        val c = client(fake)
        assertTrue(c.claim().ok)
        assertEquals(fake.calls[0].body["request_id"], fake.calls[1].body["request_id"]); assertTrue(fake.calls[2].path.startsWith(PlaybackWriterClient.STATE))
        assertEquals("network", c.progress(1.0, 2.0, true).reason); assertEquals(Status.WRITING, c.status); assertTrue(c.unsaved)
        val cancelling = Fake { _, _, _ -> throw CancellationException("cancelled") }
        assertThrows(CancellationException::class.java) { runBlocking { client(cancelling).claim() } }
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
        assertEquals(listOf(1L, 2L, 3L, 4L), fake.calls.drop(1).map { it.body["sequence"] })
    }

    @Test fun fencedExpiredAndSessionRequiredStopWritingUntilAnExplicitReclaim() = runBlocking<Unit> {
        for (reason in listOf("fenced", "expired", "session_required")) {
            var n = 0L
            val fake = Fake { path, _, _ -> if (path.endsWith("/claim")) ok(++n) else conflict(reason, 7L, "<script>TV</script>") }
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
        good.stateAnswer = { state(2, true, "电视播放器 7F3A") }
        assertTrue(client(good).claim().ok)
        val replay = Fake { _, _, _ -> ok(2, 999.0, "replayed" to true) }
        replay.stateAnswer = { state(3, true, "网页播放器 1A2B") }
        val c = client(replay)
        val r = c.claim()
        assertFalse(r.ok); assertEquals("verify_failed", r.reason); assertEquals(Status.STALE, c.status); assertEquals("网页播放器 1A2B", c.conflict?.device)
    }

    @Test fun anOlderServerLeavesTheClientUnsupportedBeforeAnyClaim() = runBlocking<Unit> {
        val fake = Fake { _, _, _ -> throw AssertionError("must not be called") }
        fake.stateAnswer = { Response(503, mapOf("reason" to "disabled")) }
        val c = client(fake)
        assertFalse(c.state().supported); assertEquals(Status.UNSUPPORTED, c.status); assertEquals("unsupported", c.claim().reason); assertEquals(1, fake.calls.size)
    }
}
