package com.fanyu.tv.ui

import android.view.KeyEvent as AndroidKey
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.fanyu.tv.data.Ep
import kotlinx.coroutines.delay

private val OK_KEYS = setOf(AndroidKey.KEYCODE_DPAD_CENTER, AndroidKey.KEYCODE_ENTER, AndroidKey.KEYCODE_NUMPAD_ENTER)

/**
 * OK fires on release; holding OK (or the menu key) fires [onLongClick]. A release whose press
 * happened elsewhere (the key that opened this screen) is ignored.
 */
fun Modifier.dpad(onClick: () -> Unit, onLongClick: (() -> Unit)? = null): Modifier = composed {
    var down by remember { mutableStateOf(false) }
    var longFired by remember { mutableStateOf(false) }
    onPreviewKeyEvent { e ->
        val key = e.nativeKeyEvent
        if (key.keyCode == AndroidKey.KEYCODE_MENU && onLongClick != null) {
            if (e.type == KeyEventType.KeyUp) onLongClick()
            return@onPreviewKeyEvent true
        }
        if (key.keyCode !in OK_KEYS) return@onPreviewKeyEvent false
        when (e.type) {
            KeyEventType.KeyDown -> {
                if (key.repeatCount == 0) { down = true; longFired = false }
                else if (down && !longFired && onLongClick != null && (key.isLongPress || key.eventTime - key.downTime >= 550)) {
                    longFired = true; onLongClick()
                }
            }
            KeyEventType.KeyUp -> {
                if (down && !longFired) onClick()
                down = false
            }
        }
        true
    }.pointerInput(onClick, onLongClick) {
        detectTapGestures(onTap = { onClick() }, onLongPress = onLongClick?.let { { _ -> it() } })
    }
}

/** A focusable surface that reports its focus to [content]. */
@Composable
fun Focusable(
    modifier: Modifier = Modifier,
    onClick: () -> Unit = {},
    onLongClick: (() -> Unit)? = null,
    onFocus: () -> Unit = {},
    requester: FocusRequester? = null,
    scale: Float = 1.06f,
    content: @Composable (focused: Boolean) -> Unit,
) {
    var focused by remember { mutableStateOf(false) }
    val s by animateFloatAsState(if (focused) scale else 1f, label = "focus-scale")
    Box(
        modifier
            .graphicsLayer { scaleX = s; scaleY = s }
            .let { if (requester != null) it.focusRequester(requester) else it }
            .onFocusChanged { focused = it.isFocused; if (it.isFocused) onFocus() }
            .dpad(onClick, onLongClick)
            .focusable()
    ) { content(focused) }
}

@Composable
fun Cover(image: String?, title: String, modifier: Modifier = Modifier) {
    Box(modifier.background(C.layer2), contentAlignment = Alignment.Center) {
        BasicText(title.trim().take(1).ifEmpty { "番" }, style = TextStyle(color = C.dim, fontSize = 32.sp, fontWeight = FontWeight.SemiBold))
        if (image != null) AsyncImage(image, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
    }
}

@Composable
fun TvButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    icon: Glyph? = null,
    primary: Boolean = false,
    requester: FocusRequester? = null,
    onFocus: () -> Unit = {},
) {
    Focusable(modifier, onClick = onClick, requester = requester, onFocus = onFocus) { focused ->
        val bg = when { primary -> C.signal; focused -> C.ink; else -> Color(0x66000000) }
        val fg = when { primary -> C.onSignal; focused -> Color(0xFF111111); else -> C.ink }
        Row(
            Modifier
                .let { if (primary && focused) it.border(3.dp, C.ink).padding(3.dp) else it }
                .background(bg)
                .let { if (!primary && !focused) it.border(1.dp, Color(0x55FFFFFF)) else it }
                .height(40.dp)
                .padding(horizontal = 16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (icon != null) Icon(icon, fg, 18.dp)
            BasicText(text, style = TextStyle(color = fg, fontSize = 15.sp, fontWeight = FontWeight.Medium))
        }
    }
}

@Composable
fun Tag(text: String) {
    BasicText(text, Modifier.border(1.dp, C.lineStrong).padding(horizontal = 6.dp, vertical = 1.dp), style = T.small.copy(color = C.ink))
}

/** Episode chip in the home hero, the same encoding as the web board: colour never alone. */
@Composable
fun EpChip(ep: Ep) {
    val (label, color, dashed) = when (ep.s) {
        "missing" -> Triple("${ep.n} 缺", C.warn, false)
        "downloading" -> Triple("${ep.n} ${ep.progress}%", C.signal, false)
        "future", "aired" -> Triple("${ep.n}", C.dim, true)
        "watched" -> Triple("${ep.n} ✓", C.dim, false)
        else -> Triple("${ep.n}", C.ink, false)
    }
    BasicText(
        label,
        Modifier
            .dashedBorder(if (color == C.ink) C.lineStrong else color, dashed)
            .widthIn(min = 30.dp)
            .padding(horizontal = 7.dp, vertical = 3.dp),
        style = TextStyle(color = color, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, textAlign = androidx.compose.ui.text.style.TextAlign.Center),
    )
}

fun Modifier.dashedBorder(color: Color, dashed: Boolean, width: Dp = 1.dp): Modifier =
    if (!dashed) border(width, color) else drawBehind {
        val px = width.toPx()
        drawRect(
            color, topLeft = Offset(px / 2, px / 2), size = Size(size.width - px, size.height - px),
            style = Stroke(px, pathEffect = PathEffect.dashPathEffect(floatArrayOf(px * 4, px * 3))),
        )
    }

@Composable
fun ProgressLine(fraction: Float, modifier: Modifier = Modifier, height: Dp = 4.dp, track: Color = C.line) {
    Box(modifier.height(height).background(track)) {
        Box(Modifier.fillMaxWidth(fraction.coerceIn(0f, 1f)).height(height).background(C.signal))
    }
}

@Composable
fun KeyHints(vararg hints: Pair<String, String>, modifier: Modifier = Modifier) {
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(24.dp), verticalAlignment = Alignment.CenterVertically) {
        for ((key, action) in hints) Row(verticalAlignment = Alignment.CenterVertically) {
            BasicText(key, Modifier.border(1.dp, C.lineStrong).padding(horizontal = 5.dp), style = T.label.copy(color = C.muted, fontWeight = FontWeight.SemiBold))
            BasicText(action, Modifier.padding(start = 6.dp), style = T.label)
        }
    }
}

/** A message at the top of the screen for a few seconds, never taking focus. */
class ToastState {
    var text by mutableStateOf<String?>(null)
    var serial by mutableStateOf(0)
    fun show(message: String) { text = message; serial++ }
}

@Composable
fun Toast(state: ToastState, modifier: Modifier = Modifier) {
    val text = state.text ?: return
    LaunchedEffect(state.serial) { delay(3000); state.text = null }
    Box(modifier.fillMaxWidth().padding(top = 24.dp), contentAlignment = Alignment.TopCenter) {
        BasicText(text, Modifier.background(Color(0xE6000000)).padding(horizontal = 18.dp, vertical = 8.dp), style = T.body.copy(fontWeight = FontWeight.Medium))
    }
}

enum class Glyph { Play, Pause, Next, Danmu, Subtitle, Check, Gear, Back }

@Composable
fun Icon(glyph: Glyph, color: Color, size: Dp, modifier: Modifier = Modifier) {
    Canvas(modifier.size(size)) {
        scale(this.size.width / 24f, pivot = Offset.Zero) {
            val stroke = Stroke(2.2f, join = StrokeJoin.Round)
            fun path(block: Path.() -> Unit) = Path().apply(block)
            when (glyph) {
                Glyph.Play -> drawPath(path { moveTo(7f, 4f); lineTo(20f, 12f); lineTo(7f, 20f); close() }, color)
                Glyph.Pause -> { drawRect(color, Offset(6f, 4f), Size(4f, 16f)); drawRect(color, Offset(14f, 4f), Size(4f, 16f)) }
                Glyph.Next -> { drawPath(path { moveTo(5f, 4f); lineTo(15f, 12f); lineTo(5f, 20f); close() }, color); drawRect(color, Offset(17f, 4f), Size(3f, 16f)) }
                Glyph.Danmu -> { drawPath(path { moveTo(3f, 5f); lineTo(21f, 5f); lineTo(21f, 16f); lineTo(9f, 16f); lineTo(4f, 20f); lineTo(4f, 16f); lineTo(3f, 16f); close() }, color, style = stroke); drawLine(color, Offset(7f, 9f), Offset(17f, 9f), 2.2f); drawLine(color, Offset(7f, 12f), Offset(13f, 12f), 2.2f) }
                Glyph.Subtitle -> { drawRect(color, Offset(3f, 5f), Size(18f, 14f), style = stroke); drawLine(color, Offset(7f, 14f), Offset(11f, 14f), 2.2f); drawLine(color, Offset(13f, 14f), Offset(17f, 14f), 2.2f); drawLine(color, Offset(7f, 10f), Offset(17f, 10f), 2.2f) }
                Glyph.Check -> drawPath(path { moveTo(4f, 12f); lineTo(9f, 17f); lineTo(20f, 6f) }, color, style = Stroke(2.6f, join = StrokeJoin.Round))
                Glyph.Gear -> {
                    drawCircle(color, 3.5f, Offset(12f, 12f), style = stroke)
                    for (i in 0 until 8) {
                        val a = Math.toRadians(i * 45.0); val c = Math.cos(a).toFloat(); val s = Math.sin(a).toFloat()
                        drawLine(color, Offset(12f + c * 6.5f, 12f + s * 6.5f), Offset(12f + c * 9.5f, 12f + s * 9.5f), 2.2f)
                    }
                }
                Glyph.Back -> drawPath(path { moveTo(15f, 4f); lineTo(7f, 12f); lineTo(15f, 20f) }, color, style = Stroke(2.6f, join = StrokeJoin.Round))
            }
        }
    }
}
