package com.fanyu.tv.data

import org.json.JSONArray
import org.json.JSONObject

val STATES = mapOf("wish" to "想看", "trial" to "试看", "watching" to "在追", "paused" to "暂搁", "dropped" to "弃番", "completed" to "看完")

/** One cell of the server's episode board (anime/board.py). */
data class Ep(
    val n: Int,
    /** watched · resume · ready · downloading · missing · aired · future */
    val s: String,
    val media: String?,
    val position: Double = 0.0,
    val duration: Double = 0.0,
    val progress: Int = 0,
    val date: String? = null,
) {
    val playable get() = media != null
}

data class Show(
    val id: Int,
    val title: String,
    val original: String,
    val image: String?,
    val state: String,
    val total: Int,
    val watched: Int,
    val unwatched: Int,
    val eps: List<Ep>,
    val next: Ep?,
) {
    val stateLabel get() = STATES[state] ?: ""
    fun after(n: Int) = eps.firstOrNull { it.n > n && it.playable && it.s != "watched" }
}

fun parseShows(a: JSONArray) = (0 until a.length()).map { parseShow(a.getJSONObject(it), a.getJSONObject(it)) }

/** Home rows carry the board inline; the detail endpoint nests it under "board". */
fun parseShow(s: JSONObject, board: JSONObject): Show {
    val eps = board.optJSONArray("eps")?.let { a -> (0 until a.length()).map { parseEp(a.getJSONObject(it)) } } ?: emptyList()
    return Show(
        id = s.getInt("id"),
        title = s.optString("title"),
        original = s.optString("original"),
        image = s.optString("image").takeIf { it.startsWith("http") },
        state = s.optString("state"),
        total = s.optInt("total"),
        watched = s.optInt("watched"),
        unwatched = s.optInt("unwatched"),
        eps = eps,
        next = board.optJSONObject("next")?.let(::parseEp),
    )
}

fun parseShowDetail(d: JSONObject): Show {
    val show = parseShow(d.getJSONObject("show"), d.getJSONObject("board"))
    // The detail endpoint's show row has no "unwatched"; count it from the board.
    return show.copy(unwatched = show.eps.count { it.playable && it.s != "watched" })
}

private fun parseEp(e: JSONObject) = Ep(
    n = e.getInt("n"),
    s = e.optString("s"),
    media = e.optString("media").ifEmpty { null },
    position = e.optDouble("position", 0.0).let { if (it.isNaN()) 0.0 else it },
    duration = e.optDouble("duration", 0.0).let { if (it.isNaN()) 0.0 else it },
    progress = e.optInt("progress"),
    date = e.optString("date").ifEmpty { null },
)

fun clock(seconds: Double): String {
    val t = seconds.toLong().coerceAtLeast(0)
    val h = t / 3600; val m = t % 3600 / 60; val s = t % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}
