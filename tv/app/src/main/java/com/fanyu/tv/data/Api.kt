package com.fanyu.tv.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import javax.net.ssl.SSLException
import java.util.concurrent.TimeUnit

/** [code] is the HTTP status, 0 when the server could not be reached, [TLS] when HTTPS could not be verified. */
class ApiError(val code: Int, message: String, val details: Map<String, Any?>? = null) : Exception(message) {
    val untrusted get() = code == 403 && message?.contains("不受信任") == true
    val needsPairing get() = code == 401

    companion object { const val TLS = -1 }
}

/**
 * The AnimeService HTTP API as the web UI uses it. LAN access needs no login; remote HTTPS
 * access uses the paired device cookie. Writes carry the bootstrap token and a same-origin Origin.
 */
class Api(private val prefs: Prefs) {
    val client: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(PrefsCookieJar(prefs))
        .connectTimeout(10, TimeUnit.SECONDS)
        // Danmu matching waits on remote sites; the server allows itself up to 90 s per source.
        .readTimeout(100, TimeUnit.SECONDS)
        .build()

    @Volatile private var token: String? = null
    @Volatile private var capabilities: Map<String, Any?> = emptyMap()
    @Volatile private var capabilityServer: String? = null

    val base: String get() = prefs.server.trimEnd('/')

    fun url(path: String) = base + path

    /** Optional capabilities the bootstrap advertised for the CURRENT server; another server starts from none. */
    val negotiatedCapabilities: Map<String, Any?> get() = if (capabilityServer == base) capabilities else emptyMap()

    suspend fun bootstrap(): JSONObject {
        val server = base                                   // bound before the request: a switch mid-flight never credits the new server
        val d = JSONObject(call(Request.Builder().url(server + "/api/bootstrap").build()))
        if (base != server) throw ApiError(0, "服务器已切换")
        token = d.optString("token")
        capabilities = capabilityWhitelist(d.optJSONObject("capabilities"))
        capabilityServer = server
        return d
    }

    /** The server changed or pairing ended: nothing negotiated or issued for the old server may be reused. */
    fun forgetServer() { token = null; capabilities = emptyMap(); capabilityServer = null }

    /** A structured reply: HTTP status (0 when unreachable) and the JSON object body when there is one. */
    class StructuredReply(val status: Int, val body: Map<String, Any?>?)

    /**
     * Writer traffic keeps the structured 409/503 bodies instead of throwing; a 403 refreshes the token
     * once and retries the SAME request, like post(). A GET is sent when [body] is null.
     */
    suspend fun structured(path: String, body: Map<String, Any?>?, server: String = base): StructuredReply {
        if (base != server) return StructuredReply(SERVER_CHANGED, null)
        if (token == null && body != null) bootstrap()
        val first = exchange(path, body, server)
        if (first.status != 403 || body == null) return first
        if ((first.body?.get("error") as? String)?.contains("不受信任") == true) return first
        bootstrap()
        if (base != server) return StructuredReply(SERVER_CHANGED, null)   // never replay the packet against another server
        return exchange(path, body, server)
    }

    private suspend fun exchange(path: String, body: Map<String, Any?>?, server: String): StructuredReply = withContext(Dispatchers.IO) {
        if (base != server) return@withContext StructuredReply(SERVER_CHANGED, null)
        val request = if (body == null) Request.Builder().url(server + path).header("X-Anime-Token", token.orEmpty()).build()
                      else Request.Builder().url(server + path).header("Origin", origin()).header("X-Anime-Token", token.orEmpty())
                          .post(JSONObject(body).toString().toRequestBody(JSON)).build()
        try {
            client.newCall(request).execute().use { r ->
                val text = r.body?.string().orEmpty()
                val parsed = try { toMap(JSONObject(text)) } catch (e: org.json.JSONException) { null }
                StructuredReply(r.code, parsed)
            }
        } catch (e: IOException) {   // SSL failures are IOExceptions too: unreachable, never a success
            StructuredReply(0, null)
        }
    }

    suspend fun get(path: String): String = call(Request.Builder().url(url(path)).build())
    suspend fun getObject(path: String) = JSONObject(get(path))
    suspend fun getArray(path: String) = JSONArray(get(path))

    suspend fun post(path: String, body: JSONObject = JSONObject()): JSONObject {
        if (token == null && path != LOGIN) bootstrap()
        return try {
            JSONObject(call(postRequest(path, body)))
        } catch (e: ApiError) {
            // The server issues a new token on every restart.
            if (e.code != 403 || e.untrusted || path == LOGIN) throw e
            bootstrap()
            JSONObject(call(postRequest(path, body)))
        }
    }

    /** "tv" asks the server for the long session a box in someone else's home needs. */
    suspend fun pair(code: String) {
        post(LOGIN, JSONObject().put("code", code.replace(" ", "").replace("-", "")).put("device", "tv"))
        bootstrap()
    }

    private fun postRequest(path: String, body: JSONObject) = Request.Builder().url(url(path))
        .header("Origin", origin())
        .header("X-Anime-Token", token.orEmpty())
        .post(body.toString().toRequestBody(JSON))
        .build()

    private fun origin(): String {
        val u = base.toHttpUrlOrNull() ?: return base
        val defaultPort = HttpUrl.defaultPort(u.scheme)
        return u.scheme + "://" + u.host + if (u.port == defaultPort) "" else ":" + u.port
    }

    private suspend fun call(request: Request): String = withContext(Dispatchers.IO) {
        try {
            client.newCall(request).execute().use { r -> read(r) }
        } catch (e: SSLException) {
            throw ApiError(ApiError.TLS, "HTTPS 证书无法验证")
        } catch (e: IOException) {
            throw ApiError(0, "连不上番屿服务")
        }
    }

    private fun read(r: Response): String {
        val text = r.body?.string().orEmpty()
        if (r.isSuccessful) return text
        val parsed = runCatching { JSONObject(text) }.getOrNull()
        val message = parsed?.let { it.optString("error").ifEmpty { it.optString("detail") } }
        throw ApiError(r.code, message?.ifEmpty { null } ?: "服务返回 ${r.code}", parsed?.let { toMap(it) })   // structured 409 reasons stay available
    }

    companion object {
        private const val LOGIN = "/api/access/login"
        private val JSON = "application/json".toMediaType()
        /** Structured reply status: the server bound to this request changed underneath it. */
        const val SERVER_CHANGED = -2

        /** Only the fields the app knows are kept from an optional capabilities object. */
        fun capabilityWhitelist(c: JSONObject?): Map<String, Any?> {
            val w = c?.optJSONObject("playback_writer") ?: return emptyMap()
            return mapOf("playback_writer" to mapOf("protocol" to w.opt("protocol"), "enabled" to w.opt("enabled")))
        }

        fun toMap(o: JSONObject): Map<String, Any?> = o.keys().asSequence().associateWith { k -> fromJson(o.get(k)) }

        private fun fromJson(v: Any?): Any? = when (v) {
            is JSONObject -> toMap(v)
            is JSONArray -> (0 until v.length()).map { fromJson(v.get(it)) }
            JSONObject.NULL -> null
            else -> v
        }

        /**
         * "192.168.1.20" → "http://192.168.1.20:4871" (LAN); "anime.example.com:38443" →
         * "https://anime.example.com:38443" (remote). An explicit scheme or port is kept.
         */
        fun normalize(input: String): String? {
            var s = input.trim().trimEnd('/')
            if (s.isEmpty() || s == "https:" || s == "http:") return null
            if (!s.startsWith("http://") && !s.startsWith("https://")) {
                val host = s.substringBefore('/').substringBefore(':')
                val lan = host == "localhost" || Regex("^\\d{1,3}(\\.\\d{1,3}){3}$").matches(host)
                s = (if (lan) "http://" else "https://") + s
            }
            val u = s.toHttpUrlOrNull() ?: return null
            if (u.host.isEmpty()) return null
            val explicitPort = Regex("^https?://[^/]+:\\d+").containsMatchIn(s)
            val port = if (explicitPort || u.scheme == "https") u.port else 4871
            val defaultPort = HttpUrl.defaultPort(u.scheme)
            return u.scheme + "://" + u.host + if (port == defaultPort) "" else ":$port"
        }
    }
}

private class PrefsCookieJar(private val prefs: Prefs) : CookieJar {
    private var cache: MutableList<Cookie>? = null

    @Synchronized
    private fun all(): MutableList<Cookie> = cache ?: prefs.cookies.mapNotNull { line ->
        val split = line.indexOf('\n')
        if (split < 0) null else line.substring(0, split).toHttpUrlOrNull()?.let { Cookie.parse(it, line.substring(split + 1)) }
    }.toMutableList().also { cache = it }

    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        val list = all()
        for (c in cookies) {
            list.removeAll { it.name == c.name && it.domain == c.domain && it.path == c.path }
            if (c.expiresAt > System.currentTimeMillis()) list.add(c)
        }
        prefs.cookies = list.map { (if (it.secure) "https://" else "http://") + it.domain + "/\n" + it }.toSet()
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val now = System.currentTimeMillis()
        return all().filter { it.expiresAt > now && it.matches(url) }
    }
}
