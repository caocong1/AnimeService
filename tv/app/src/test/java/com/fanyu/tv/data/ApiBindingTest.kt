package com.fanyu.tv.data

import com.fanyu.tv.player.PlaybackWriterClient
import com.fanyu.tv.player.WriterTransport
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Api and WriterTransport against two real HTTP servers whose replies the test releases, so a switch
 * (A→B, A→B→A, a failed switch rolled back, the same server forgotten) happens while a request, a
 * bootstrap or a token refresh is in flight: nothing issued for one generation is sent to, credited to
 * or replayed against another.
 */
class ApiBindingTest {
    private class Store(override var server: String, override var cookies: Set<String> = emptySet()) : ServerStore

    /** One server stand-in: bootstrap issues "<name>-<n>"; a write must carry the current token (403 otherwise). */
    private class Scripted(val name: String) : Dispatcher() {
        @Volatile var expected = ""
        @Volatile var issued = 0
        @Volatile var bootstrapStatus = 200
        @Volatile var hold: CountDownLatch? = null            // every request waits here while set
        @Volatile var holdBootstrap: CountDownLatch? = null   // only the bootstrap waits
        @Volatile var holdAfterBootstrap: CountDownLatch? = null   // becomes [hold] once a bootstrap was answered (holds the retry)
        @Volatile var bootstrapBody: String? = null           // a custom 200 bootstrap body; @TOKEN@ is the issued token
        fun restart() { expected = "$name-restarted" }        // the real server issues a new token on every restart

        override fun dispatch(request: RecordedRequest): MockResponse {
            hold?.await()
            val path = request.path.orEmpty().substringBefore('?')
            if (path == "/api/bootstrap") {
                holdBootstrap?.await()
                if (bootstrapStatus != 200) return json(bootstrapStatus, """{"error":"down"}""")
                expected = "$name-${++issued}"
                hold = holdAfterBootstrap
                val body = bootstrapBody?.replace("@TOKEN@", expected)
                    ?: """{"token":"$expected","capabilities":{"playback_writer":{"protocol":"playback-writer/v2","enabled":true}}}"""
                return json(200, body)
            }
            if (request.method == "POST" && request.getHeader("X-Anime-Token") != expected) return json(403, """{"error":"token"}""")
            return when (path) {
                PlaybackWriterClient.STATE -> json(200, """{"media_key":"$KEY","requested_media_key":"$KEY","generation":1,"active":false,"expires":0}""")
                PlaybackWriterClient.CLAIM -> json(200, """{"ok":true,"media_key":"$KEY","requested_media_key":"$KEY","generation":1,"expires":999}""")
                PlaybackWriterClient.PROGRESS -> json(200, """{"ok":true,"generation":1,"sequence":${JSONObject(request.body.clone().readUtf8()).optLong("sequence")}}""")
                else -> json(200, "{}")
            }
        }

        private fun json(code: Int, body: String) = MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)
    }

    private class Box(name: String) {
        val script = Scripted(name)
        val server = MockWebServer().apply { dispatcher = script; start() }
        val url = server.url("/").toString().trimEnd('/')
        /** The next request received, in order, whether or not it was answered yet. */
        fun next(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS) ?: throw AssertionError("$url received nothing")
        fun quiet() { server.takeRequest(300, TimeUnit.MILLISECONDS)?.let { throw AssertionError("$url received ${it.path}") } }
        val received: Int get() = server.requestCount
    }

    private val a = Box("A")
    private val b = Box("B")
    private val store = Store(a.url)
    private val api = Api(store)
    private val packet = mapOf("media_key" to KEY, "session" to SESSION, "generation" to 1L, "writer_token" to "t".repeat(32), "sequence" to 1L)

    @After fun down() {
        for (box in listOf(a, b)) { box.script.hold?.countDown(); box.script.holdBootstrap?.countDown(); box.script.holdAfterBootstrap?.countDown(); box.server.shutdown() }
    }

    private fun switched(e: Throwable?) = e is ApiError && e.message == "服务器已切换"

    /** A legacy write held at A while [meanwhile] runs; returns what the caller got. */
    private suspend fun legacyInFlight(meanwhile: () -> Unit): Result<JSONObject> = coroutineScope {
        val gate = CountDownLatch(1); a.script.hold = gate
        val r = async(Dispatchers.IO) { runCatching { api.post("/api/web/media/x/progress", JSONObject().put("position", 1)) } }
        a.next(); meanwhile(); gate.countDown(); a.script.hold = null
        r.await()
    }

    companion object {
        const val KEY = "0123456789abcdef0123456789abcdef"
        const val SESSION = "session-aaaaaaaaaaaaaaaa"
    }

    @Test fun bootstrapIssuesForOneGenerationAndEverySwitchDropsIt() = runBlocking<Unit> {   // A→B, then A→B→A
        api.bootstrap(); a.next()
        val onA = api.context()
        assertEquals("A-1", onA.token); assertTrue(PlaybackWriterClient.negotiated(api.negotiatedCapabilities))
        store.server = b.url
        assertNull(api.context().token); assertTrue(api.negotiatedCapabilities.isEmpty()); assertFalse(api.accepts(onA))
        assertEquals(200, api.structured(PlaybackWriterClient.CLAIM, packet).status)       // B bootstraps for itself
        assertEquals("/api/bootstrap", b.next().path)
        val claim = b.next()
        assertEquals("B-1", claim.getHeader("X-Anime-Token")); assertEquals(b.url, claim.getHeader("Origin")); assertEquals(b.url + PlaybackWriterClient.CLAIM, claim.requestUrl.toString())
        assertEquals(1, a.received)
        store.server = a.url                                                                // back to A: a new generation of A
        assertNull(api.context().token); assertFalse(api.accepts(onA))
        assertEquals(Api.SERVER_CHANGED, api.structured(PlaybackWriterClient.PROGRESS, packet, onA).status)   // the old binding sends nothing
        assertEquals(1, a.received)
        assertEquals(200, api.structured(PlaybackWriterClient.PROGRESS, packet).status)
        assertEquals("/api/bootstrap", a.next().path); assertEquals("A-2", a.next().getHeader("X-Anime-Token"))   // nothing from before is reused
    }

    @Test fun aReplyArrivingAfterTheSwitchIsDropped() = runBlocking<Unit> {
        api.bootstrap(); a.next()
        val gate = CountDownLatch(1); a.script.hold = gate
        val late = async(Dispatchers.IO) { api.structured(PlaybackWriterClient.PROGRESS, packet) }
        assertEquals(PlaybackWriterClient.PROGRESS, a.next().path)                          // in flight at A
        store.server = b.url                                                                // the user switched meanwhile
        gate.countDown()
        val r = late.await()
        assertEquals(Api.SERVER_CHANGED, r.status); assertNull(r.body)
        assertEquals(0, b.received); assertNull(api.context().token)
    }

    @Test fun anOldBootstrapNeverCreditsTheNewServer() = runBlocking<Unit> {
        val gate = CountDownLatch(1); a.script.holdBootstrap = gate
        val old = async(Dispatchers.IO) { runCatching { api.bootstrap() } }
        assertEquals("/api/bootstrap", a.next().path)
        store.server = b.url
        gate.countDown()
        val e = old.await().exceptionOrNull()
        assertTrue("$e", e is ApiError && e.message == "服务器已切换")
        assertNull(api.context().token); assertTrue(api.negotiatedCapabilities.isEmpty()); assertEquals(0, b.received)
    }

    @Test fun forgettingTheSameServerIsANewGeneration() = runBlocking<Unit> {
        api.bootstrap(); a.next()
        val before = api.context()
        api.forgetServer()
        assertNull(api.context().token); assertFalse(api.accepts(before))
        assertEquals(Api.SERVER_CHANGED, api.structured(PlaybackWriterClient.PROGRESS, packet, before).status); assertEquals(1, a.received)
        assertEquals(200, api.structured(PlaybackWriterClient.PROGRESS, packet).status)
        assertEquals("/api/bootstrap", a.next().path); assertEquals("A-2", a.next().getHeader("X-Anime-Token"))
    }

    @Test fun aFailedSwitchRolledBackReissuesNothingFromBefore() = runBlocking<Unit> {
        api.bootstrap(); a.next()
        val before = api.context()
        b.script.bootstrapStatus = 503
        store.server = b.url
        assertEquals(503, assertThrows(ApiError::class.java) { runBlocking { api.bootstrap() } }.code)
        store.server = a.url; api.forgetServer()                                            // the connect screen rolls back
        assertNull(api.context().token); assertFalse(api.accepts(before))
        assertEquals(Api.SERVER_CHANGED, api.structured(PlaybackWriterClient.PROGRESS, packet, before).status); assertEquals(1, a.received)
        assertEquals(200, api.structured(PlaybackWriterClient.PROGRESS, packet).status)
        assertEquals("/api/bootstrap", a.next().path); assertEquals("A-2", a.next().getHeader("X-Anime-Token"))
    }

    @Test fun a403RefreshesTheTokenOnceWithinTheSameGenerationOnly() = runBlocking<Unit> {
        api.bootstrap(); a.next()
        a.script.restart()
        assertEquals(200, api.structured(PlaybackWriterClient.PROGRESS, packet).status)
        assertEquals("A-1", a.next().getHeader("X-Anime-Token")); assertEquals("/api/bootstrap", a.next().path); assertEquals("A-2", a.next().getHeader("X-Anime-Token"))
        a.script.restart()
        val gate = CountDownLatch(1); a.script.holdBootstrap = gate
        val moved = async(Dispatchers.IO) { api.structured(PlaybackWriterClient.PROGRESS, packet) }
        a.next(); assertEquals("/api/bootstrap", a.next().path)                             // the 403 came back; the refresh is in flight
        store.server = b.url
        gate.countDown()
        assertEquals(Api.SERVER_CHANGED, moved.await().status); assertEquals(0, b.received)  // the packet is never replayed elsewhere
        store.server = a.url; a.script.holdBootstrap = null                                 // the legacy write path binds the same way
        api.bootstrap(); a.next(); a.script.restart()
        val gate2 = CountDownLatch(1); a.script.holdBootstrap = gate2
        val legacy = async(Dispatchers.IO) { runCatching { api.post("/api/web/media/x/progress", JSONObject().put("position", 1)) } }
        a.next(); assertEquals("/api/bootstrap", a.next().path)
        store.server = b.url
        gate2.countDown()
        val e = legacy.await().exceptionOrNull()
        assertTrue("$e", e is ApiError && e.message == "服务器已切换"); assertEquals(0, b.received)
    }

    @Test fun aLegacyReplyIsDeliveredOnlyForItsOwnConnection() = runBlocking<Unit> {   // owner round-22 group 3
        api.bootstrap(); a.next()
        assertTrue(switched(legacyInFlight { store.server = b.url; api.forgetServer() }.exceptionOrNull()))   // a 200 of the old connection after A→B
        assertEquals(0, b.received); a.quiet()
        store.server = a.url; api.forgetServer(); api.bootstrap(); a.next()                                   // A→B→A as the connect screen does it
        assertTrue(switched(legacyInFlight { store.server = b.url; api.forgetServer(); store.server = a.url; api.forgetServer() }.exceptionOrNull()))
        api.bootstrap(); a.next()
        assertTrue(switched(legacyInFlight { api.forgetServer() }.exceptionOrNull()))                         // the same base, forgotten
        api.bootstrap(); a.next(); a.script.restart()
        assertTrue(switched(legacyInFlight { store.server = b.url; api.forgetServer() }.exceptionOrNull()))   // a late 403 bootstraps nothing anywhere
        assertEquals(0, b.received); a.quiet()
        store.server = a.url; api.forgetServer(); api.bootstrap(); a.next(); a.script.restart()               // the retry's 200 after a switch
        val gate = CountDownLatch(1); a.script.holdAfterBootstrap = gate
        val retry = async(Dispatchers.IO) { runCatching { api.post("/api/web/media/x/progress", JSONObject().put("position", 1)) } }
        a.next(); assertEquals("/api/bootstrap", a.next().path); assertEquals("/api/web/media/x/progress", a.next().path)   // the retry is held
        store.server = b.url; api.forgetServer()
        gate.countDown(); a.script.hold = null; a.script.holdAfterBootstrap = null
        assertTrue(switched(retry.await().exceptionOrNull())); assertEquals(0, b.received)
        store.server = a.url; api.forgetServer(); api.bootstrap(); a.next()                                   // and the plain success path still works
        assertEquals("{}", api.post("/api/web/media/x/progress", JSONObject().put("position", 1)).toString()); a.next(); a.quiet()
    }

    @Test fun aFailedOrWithdrawnRefreshReachesTheWriterWithItsMeaning() = runBlocking<Unit> {   // owner round-22 group 4
        class Case(val status: Int, val body: String?, val reason: String, val halted: Boolean)
        val cases = listOf(
            Case(401, null, "unauthorized_401", true), Case(403, null, "unauthorized_403", true), Case(404, null, "protocol_404", true),
            Case(503, null, "disabled_503", true), Case(200, "not json", "protocol_200", true),
            Case(200, """{"token":"@TOKEN@","capabilities":{}}""", "disabled", true),
            Case(200, """{"token":"@TOKEN@","capabilities":{"playback_writer":{"protocol":"playback-writer/v3","enabled":true}}}""", "disabled", true),
            Case(500, null, "server_500", false),
        )
        for (case in cases) {
            api.forgetServer(); a.script.bootstrapStatus = 200; a.script.bootstrapBody = null
            api.bootstrap(); a.next()
            val c = PlaybackWriterClient(KEY, SESSION, "电视播放器 7F3A", WriterTransport(api))
            assertTrue(c.claim().ok); a.next()
            a.script.restart(); a.script.bootstrapStatus = case.status; a.script.bootstrapBody = case.body   // the refresh after the 403 fails or withdraws
            val r = c.progress(1.0, 2.0, true)
            assertEquals(case.reason, r.reason)
            assertEquals(case.reason, if (case.halted) PlaybackWriterClient.Status.HALTED else PlaybackWriterClient.Status.WRITING, c.status)
            a.next(); assertEquals(case.reason, "/api/bootstrap", a.next().path); a.quiet()                  // the packet is never replayed
            if (case.halted) { assertEquals("halted", c.claim().reason); a.quiet() }
        }
        api.forgetServer(); a.script.bootstrapStatus = 200; a.script.bootstrapBody = null                   // a valid refresh in the same generation
        api.bootstrap(); a.next()
        val c = PlaybackWriterClient(KEY, SESSION, "电视播放器 7F3A", WriterTransport(api))
        assertTrue(c.claim().ok); a.next()
        a.script.restart()
        val r = c.progress(1.0, 2.0, true)
        assertTrue(r.ok); assertEquals(1L, r.sequence); assertEquals(PlaybackWriterClient.Status.WRITING, c.status)
        a.next(); assertEquals("/api/bootstrap", a.next().path); assertEquals(a.script.expected, a.next().getHeader("X-Anime-Token")); a.quiet()
    }

    @Test fun theWriterTransportIsBoundToOneGenerationAndHaltsWhenItMoves() = runBlocking<Unit> {
        api.bootstrap(); a.next()
        val onA = api.context()
        val c = PlaybackWriterClient(KEY, SESSION, "电视播放器 7F3A", WriterTransport(api))
        assertTrue(c.claim().ok); assertEquals("A-1", a.next().getHeader("X-Anime-Token"))
        val gate = CountDownLatch(1); a.script.hold = gate
        val late = async(Dispatchers.IO) { c.progress(1.0, 2.0, true) }
        assertEquals(PlaybackWriterClient.PROGRESS, a.next().path)
        store.server = b.url
        gate.countDown()
        assertEquals("protocol", late.await().reason); assertEquals(PlaybackWriterClient.Status.HALTED, c.status)
        assertEquals("halted", c.claim().reason); assertEquals(0, b.received)
        store.server = a.url; a.script.hold = null                                          // A→B→A: a transport bound before sends nothing
        val c2 = PlaybackWriterClient(KEY, SESSION, "电视播放器 7F3A", WriterTransport(api, onA))
        assertEquals("protocol", c2.claim().reason); assertEquals(PlaybackWriterClient.Status.HALTED, c2.status); assertEquals(3, a.received)
    }
}
