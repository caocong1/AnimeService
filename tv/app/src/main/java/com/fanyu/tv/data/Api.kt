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
class ApiError(val code: Int, message: String) : Exception(message) {
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

    val base: String get() = prefs.server.trimEnd('/')

    fun url(path: String) = base + path

    suspend fun bootstrap(): JSONObject {
        val d = JSONObject(call(Request.Builder().url(url("/api/bootstrap")).build()))
        token = d.optString("token")
        return d
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
        val message = runCatching { JSONObject(text) }.getOrNull()?.let { it.optString("error").ifEmpty { it.optString("detail") } }
        throw ApiError(r.code, message?.ifEmpty { null } ?: "服务返回 ${r.code}")
    }

    companion object {
        private const val LOGIN = "/api/access/login"
        private val JSON = "application/json".toMediaType()

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
