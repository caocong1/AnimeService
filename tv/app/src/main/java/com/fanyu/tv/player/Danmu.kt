package com.fanyu.tv.player

import android.graphics.Color
import com.fanyu.tv.data.Api
import com.fanyu.tv.data.ApiError
import com.fanyu.tv.data.Prefs
import kotlinx.coroutines.delay
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.roundToLong

class DanmuSource(
    val id: String,
    val identity: String,
    val site: String,
    val title: String,
    val comments: List<Danmaku>,
    val error: String?,
    /** Seconds; restored from an earlier alignment or manual edit, otherwise 0. */
    var offset: Double,
    val saved: Boolean,
) {
    var alignment = ""
    val name get() = SITE_NAMES[site] ?: if (Regex("bilibili|B站", RegexOption.IGNORE_CASE).containsMatchIn(title)) "B站" else "弹幕"
    val bilibili get() = site == "bilibili" || Regex("bilibili|B站", RegexOption.IGNORE_CASE).containsMatchIn(title)
}

class DanmuResult(val sources: List<DanmuSource>, val message: String)

private val SITE_NAMES = mapOf("bilibili" to "B站", "bahamut" to "巴哈姆特", "iqiyi" to "爱奇艺", "youku" to "优酷", "tencent" to "腾讯视频")

/** The web player's automatic flow: match this episode, then align Bilibili sources by audio. */
object Danmu {
    suspend fun load(api: Api, prefs: Prefs, media: String): DanmuResult {
        val d = api.getObject("/api/web/media/$media/danmu")
        if (d.optString("status") != "matched") return DanmuResult(emptyList(), d.optString("message").ifEmpty { "未匹配到本集弹幕" })
        val selected = d.optJSONArray("selected")
        val byId = HashMap<String, JSONObject>()
        d.optJSONArray("sources")?.let { a -> for (i in 0 until a.length()) a.getJSONObject(i).let { byId[it.optString("id")] = it } }
        val sources = (0 until (selected?.length() ?: 0)).mapNotNull { i ->
            val s = selected!!.getJSONObject(i)
            val src = byId[s.optString("id")] ?: return@mapNotNull null
            val identity = s.optString("source_identity").ifEmpty { src.optString("source_identity") }
            val saved = prefs.sourceOffset(media, identity)
            DanmuSource(
                id = s.optString("id"), identity = identity, site = src.optString("site"), title = s.optString("title"),
                comments = parse(src), error = src.optString("error").ifEmpty { null },
                offset = saved ?: 0.0, saved = saved != null,
            )
        }
        return DanmuResult(sources, d.optString("message"))
    }

    private fun parse(src: JSONObject): List<Danmaku> {
        val a = src.optJSONArray("comments") ?: return emptyList()
        return (0 until a.length()).mapNotNull { i ->
            val c = a.optJSONObject(i) ?: return@mapNotNull null
            val time = c.optDouble("time", -1.0)
            val text = c.optString("text")
            if (time < 0 || text.isEmpty()) return@mapNotNull null
            val color = runCatching { Color.parseColor(c.optString("color", "#ffffff")) }.getOrDefault(Color.WHITE)
            Danmaku((time * 1000).roundToLong(), text, c.optInt("mode", 0), color)
        }
    }

    /** Polls the server's audio alignment; returns the offset in seconds when it is reliable. */
    suspend fun align(api: Api, media: String, source: DanmuSource, onStatus: (String) -> Unit): Double? {
        val base = "/api/web/media/$media/alignment"
        try {
            onStatus("等待音频对齐…")
            var r = api.post(base, JSONObject().put("source_id", source.id).put("force", false).put("confirmed", false))
            val job = r.optString("job_id")
            var polls = 0
            while (r.optString("status") in setOf("queued", "running")) {
                onStatus(if (r.optString("status") == "running") "正在音频对齐…" else "等待音频对齐…")
                if (job.isEmpty() || ++polls > 100) { onStatus("自动对齐超时"); return null }
                delay(1500)
                r = api.getObject("$base/$job")
            }
            val offset = r.optDouble("offset", Double.NaN)
            return when (r.optString("status")) {
                "matched" -> if (!offset.isNaN() && abs(offset) <= 3600) (Math.round(offset * 10) / 10.0).also { onStatus("已对齐 ${signed(it)}s") } else null
                "unreliable" -> { onStatus("音频未能可靠匹配"); null }
                else -> { onStatus(r.optString("message").ifEmpty { "自动对齐不可用" }.take(40)); null }
            }
        } catch (e: ApiError) {
            onStatus("自动对齐不可用"); return null
        }
    }

    /** Shift each source by its own offset, then drop duplicates across sources (DanmuTiming.mix). */
    fun mix(sources: List<DanmuSource>): List<Danmaku> {
        val seen = HashSet<Triple<Long, String, Int>>()
        val out = ArrayList<Danmaku>()
        for (s in sources) {
            val shift = (s.offset * 1000).roundToLong()
            for (c in s.comments) {
                val t = c.time + shift
                if (t < 0) continue
                if (seen.add(Triple(t / 100, c.text, c.mode))) out.add(Danmaku(t, c.text, c.mode, c.color))
            }
        }
        out.sortBy { it.time }
        return out
    }

    fun signed(v: Double) = (if (v > 0) "+" else "") + "%.1f".format(v)
}
