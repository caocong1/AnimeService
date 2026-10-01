package com.fanyu.tv.data

import android.content.Context

/** Everything the box remembers: the server, its session cookies and the danmu display. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("fanyu", Context.MODE_PRIVATE)

    var server: String
        get() = sp.getString("server", "") ?: ""
        set(value) = sp.edit().putString("server", value).apply()

    var cookies: Set<String>
        get() = sp.getStringSet("cookies", emptySet()) ?: emptySet()
        set(value) = sp.edit().putStringSet("cookies", value).apply()

    var danmu: DanmuSettings
        get() = DanmuSettings(
            visible = sp.getBoolean("dm_visible", true),
            size = sp.getInt("dm_size", 1),
            opacity = sp.getInt("dm_opacity", 1),
            area = sp.getInt("dm_area", 1),
            speed = sp.getInt("dm_speed", 1),
        ).clamped()
        set(value) = sp.edit()
            .putBoolean("dm_visible", value.visible).putInt("dm_size", value.size)
            .putInt("dm_opacity", value.opacity).putInt("dm_area", value.area)
            .putInt("dm_speed", value.speed).apply()

    /** Per-source offsets mirror the web player: one value per (media, source identity). */
    fun sourceOffset(media: String, identity: String): Double? =
        if (sp.contains("off:$media:$identity")) sp.getFloat("off:$media:$identity", 0f).toDouble() else null

    fun setSourceOffset(media: String, identity: String, seconds: Double) =
        sp.edit().putFloat("off:$media:$identity", seconds.toFloat()).apply()

    fun globalOffset(media: String): Double = sp.getFloat("goff:$media", 0f).toDouble()
    fun setGlobalOffset(media: String, seconds: Double) = sp.edit().putFloat("goff:$media", seconds.toFloat()).apply()
}

data class DanmuSettings(
    val visible: Boolean = true,
    val size: Int = 1,
    val opacity: Int = 1,
    val area: Int = 1,
    val speed: Int = 1,
) {
    fun clamped() = copy(
        size = size.coerceIn(SIZES.indices), opacity = opacity.coerceIn(OPACITY.indices),
        area = area.coerceIn(AREAS.indices), speed = speed.coerceIn(SPEEDS.indices),
    )

    val textSp get() = SIZES[size].second
    val alpha get() = OPACITY[opacity].second
    val areaFraction get() = AREAS[area].second
    val scrollMillis get() = SPEEDS[speed].second

    companion object {
        val SIZES = listOf("小" to 18f, "标准" to 22f, "大" to 26f, "特大" to 30f)
        val OPACITY = listOf("100%" to 1f, "90%" to .9f, "75%" to .75f, "60%" to .6f, "45%" to .45f)
        val AREAS = listOf("四分之一" to .25f, "半屏" to .5f, "四分之三" to .75f, "全屏" to 1f)
        val SPEEDS = listOf("慢" to 12000L, "适中" to 9000L, "快" to 6500L)
    }
}
