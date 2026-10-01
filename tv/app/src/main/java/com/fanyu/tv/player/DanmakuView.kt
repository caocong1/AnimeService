package com.fanyu.tv.player

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.util.TypedValue
import android.view.View
import com.fanyu.tv.data.DanmuSettings

/** A comment on the shared timeline, already shifted by its source's offset. */
class Danmaku(val time: Long, val text: String, val mode: Int, val color: Int)

/**
 * Draws comments against the player's clock, so pausing freezes them and seeking re-syncs.
 * Scrolling comments take the first lane where they cannot catch the previous one; with no
 * free lane a comment is dropped rather than overlapped (the web player's anti-overlap).
 */
class DanmakuView(context: Context) : View(context) {
    var clock: () -> Long = { 0L }
    var playing: () -> Boolean = { false }

    /** Seconds added to every comment's time; adjusted from the remote while watching. */
    var offsetMillis = 0L
        set(value) { field = value; resync() }

    var settings = DanmuSettings()
        set(value) {
            field = value
            fill.textSize = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value.textSp, resources.displayMetrics)
            stroke.textSize = fill.textSize
            resync(); invalidate()
        }

    private var items: List<Danmaku> = emptyList()
    private var cursor = 0
    private var last = Long.MIN_VALUE

    private class Active(val item: Danmaku, val lane: Int, val width: Float, val start: Long)
    private val scrolling = ArrayList<Active>()
    private val pinned = ArrayList<Active>()

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { typeface = Typeface.DEFAULT_BOLD }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.DEFAULT_BOLD; style = Paint.Style.STROKE; color = Color.BLACK
        strokeWidth = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 1.6f, resources.displayMetrics)
        strokeJoin = Paint.Join.ROUND
    }

    init { settings = DanmuSettings() }

    fun setItems(list: List<Danmaku>) {
        items = list
        resync(); invalidate()
    }

    /** Call when playback starts or the position jumps. */
    fun wake() = postInvalidateOnAnimation()

    private fun resync() {
        scrolling.clear(); pinned.clear()
        last = Long.MIN_VALUE
    }

    private fun now() = clock() - offsetMillis

    override fun onDraw(canvas: Canvas) {
        if (!settings.visible || items.isEmpty() || width == 0) return
        val t = now()
        if (last == Long.MIN_VALUE || t < last || t - last > 1500) {
            scrolling.clear(); pinned.clear()
            cursor = lowerBound(t)
        }
        while (cursor < items.size && items[cursor].time <= t) {
            // Long skips (resume, frame drops) never flood the screen with stale comments.
            if (t - items[cursor].time < 1500) emit(items[cursor], t)
            cursor++
        }
        last = t

        val alpha = (settings.alpha * 255).toInt()
        val line = lineHeight()
        val w = width.toFloat()
        val duration = settings.scrollMillis.toFloat()
        val it = scrolling.iterator()
        while (it.hasNext()) {
            val a = it.next()
            val x = w - (t - a.start) / duration * (w + a.width)
            if (x + a.width < 0) { it.remove(); continue }
            draw(canvas, a, x, line * a.lane + fill.textSize, alpha)
        }
        val p = pinned.iterator()
        val area = areaHeight()
        while (p.hasNext()) {
            val a = p.next()
            if (t - a.start > PINNED_MILLIS) { p.remove(); continue }
            val y = if (a.item.mode == TOP) line * a.lane + fill.textSize else area - line * a.lane - (line - fill.textSize)
            draw(canvas, a, (w - a.width) / 2, y, alpha)
        }
        if (playing()) postInvalidateOnAnimation()
    }

    private fun draw(canvas: Canvas, a: Active, x: Float, y: Float, alpha: Int) {
        stroke.alpha = alpha
        canvas.drawText(a.item.text, x, y, stroke)
        fill.color = a.item.color
        fill.alpha = alpha
        canvas.drawText(a.item.text, x, y, fill)
    }

    private fun lineHeight() = fill.textSize * 1.3f
    private fun areaHeight() = height * settings.areaFraction
    private fun lanes() = (areaHeight() / lineHeight()).toInt().coerceAtLeast(1)

    private fun emit(item: Danmaku, t: Long) {
        val width = fill.measureText(item.text)
        val lanes = lanes()
        if (item.mode != SCROLL) {
            for (lane in 0 until lanes) {
                if (pinned.none { it.lane == lane && it.item.mode == item.mode }) { pinned.add(Active(item, lane, width, t)); return }
            }
            return
        }
        val w = this.width.toFloat()
        val duration = settings.scrollMillis.toFloat()
        val gap = fill.textSize
        for (lane in 0 until lanes) {
            val prev = scrolling.lastOrNull { it.lane == lane }
            if (prev == null) { scrolling.add(Active(item, lane, width, t)); return }
            val prevSpeed = (w + prev.width) / duration
            val prevX = w - (t - prev.start) * prevSpeed
            val entered = prevX + prev.width + gap < w
            // The new comment must not reach the left edge before the previous one has left it.
            val prevLeaves = (prevX + prev.width) / prevSpeed
            val newArrives = w / ((w + width) / duration)
            if (entered && newArrives >= prevLeaves) { scrolling.add(Active(item, lane, width, t)); return }
        }
    }

    private fun lowerBound(t: Long): Int {
        var lo = 0; var hi = items.size
        while (lo < hi) { val mid = (lo + hi) ushr 1; if (items[mid].time < t) lo = mid + 1 else hi = mid }
        return lo
    }

    companion object {
        const val SCROLL = 0
        const val TOP = 1
        const val BOTTOM = 2
        private const val PINNED_MILLIS = 4000L
    }
}
