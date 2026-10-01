package com.fanyu.tv.update

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.fanyu.tv.BuildConfig
import com.fanyu.tv.data.Api
import com.fanyu.tv.data.ApiError
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest

data class Release(val code: Int, val name: String, val notes: List<String>, val size: Long, val sha256: String, val url: String)

/** The server publishes data/tv/{fanyu-tv.apk,release.json}; the box installs it through the system installer. */
object Updater {
    suspend fun check(api: Api): Release? {
        val d = runCatching { api.getObject("/api/tv/release") }.getOrNull() ?: return null
        val code = d.optInt("version_code", 0)
        if (code <= BuildConfig.VERSION_CODE) return null
        return parse(d, code)
    }

    private fun parse(d: JSONObject, code: Int) = Release(
        code = code,
        name = d.optString("version_name"),
        notes = d.optJSONArray("notes")?.let { a -> (0 until a.length()).map { a.optString(it) } } ?: emptyList(),
        size = d.optLong("size"),
        sha256 = d.optString("sha256"),
        url = d.optString("url", "/api/tv/release.apk"),
    )

    suspend fun download(context: Context, api: Api, release: Release, progress: (Float) -> Unit): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val file = File(dir, "fanyu-tv-${release.code}.apk")
        val digest = MessageDigest.getInstance("SHA-256")
        try {
            api.client.newCall(Request.Builder().url(api.url(release.url)).build()).execute().use { r ->
                if (!r.isSuccessful) throw ApiError(r.code, "下载失败：服务返回 ${r.code}")
                val body = r.body ?: throw ApiError(r.code, "下载失败：空响应")
                val total = release.size.takeIf { it > 0 } ?: body.contentLength()
                body.byteStream().use { input ->
                    file.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024); var done = 0L
                        while (true) {
                            ensureActive()
                            val n = input.read(buf); if (n < 0) break
                            out.write(buf, 0, n); digest.update(buf, 0, n); done += n
                            if (total > 0) progress(done.toFloat() / total)
                        }
                    }
                }
            }
        } catch (e: IOException) { throw ApiError(0, "下载中断，请重试") }
        val hex = digest.digest().joinToString("") { "%02x".format(it) }
        if (release.sha256.isNotEmpty() && !hex.equals(release.sha256, true)) { file.delete(); throw ApiError(0, "安装包校验不一致，请重新发布") }
        file
    }

    /** False when the box first has to allow installs from this app; the settings screen is opened instead. */
    fun install(context: Context, file: File): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !context.packageManager.canRequestPackageInstalls()) {
            val settings = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + context.packageName))
            try { context.startActivity(settings); return false } catch (_: ActivityNotFoundException) { /* some boxes lack the screen; try anyway */ }
        }
        val uri = FileProvider.getUriForFile(context, context.packageName + ".updates", file)
        val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }
}
