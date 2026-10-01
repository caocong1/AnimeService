package com.fanyu.tv.player

import android.view.KeyEvent as AndroidKey
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.ui.PlayerView
import com.fanyu.tv.data.DanmuSettings
import com.fanyu.tv.data.clock
import com.fanyu.tv.ui.C
import com.fanyu.tv.ui.Focusable
import com.fanyu.tv.ui.Glyph
import com.fanyu.tv.ui.Icon
import com.fanyu.tv.ui.SafeH
import com.fanyu.tv.ui.T
import com.fanyu.tv.ui.Toast
import com.fanyu.tv.ui.TvButton
import kotlinx.coroutines.delay

@Composable
fun PlayerScreen(ui: PlayerUi, playerView: PlayerView, danmaku: DanmakuView, actions: PlayerActions) {
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView({ playerView }, Modifier.fillMaxSize())
        AndroidView({ danmaku }, Modifier.fillMaxSize())

        var slowLoad by remember { mutableStateOf(false) }
        LaunchedEffect(ui.buffering) { slowLoad = false; if (ui.buffering) { delay(700); slowLoad = true } }
        if (slowLoad && ui.buffering && ui.overlay != Overlay.Error) Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            BasicText("加载中…", Modifier.background(Color(0x99000000)).padding(horizontal = 18.dp, vertical = 8.dp), style = T.body)
        }

        ui.seekPreview?.let { SeekToast(it, ui.duration) }

        var statusVisible by remember { mutableStateOf(false) }
        LaunchedEffect(ui.statusUntil) {
            val left = ui.statusUntil - System.currentTimeMillis()
            if (left > 0) { statusVisible = true; delay(left); statusVisible = false }
        }
        if (statusVisible || ui.overlay == Overlay.Controls) StatusChip(ui.danmuLine, ui.settings.visible)

        LaunchedEffect(ui.overlay, ui.interaction, ui.playing) {
            if (ui.overlay == Overlay.Controls && ui.playing) { delay(5000); ui.overlay = Overlay.None }
        }
        when (ui.overlay) {
            Overlay.Controls -> Controls(ui, actions)
            Overlay.Danmu -> DanmuDrawer(ui, actions)
            Overlay.Subtitles -> SubtitleDrawer(ui, actions)
            Overlay.End -> EndPanel(ui, actions)
            Overlay.Error -> ErrorPanel(ui.error, actions)
            Overlay.None -> {}
        }
        Toast(ui.toast)
    }
}

@Composable
private fun SeekToast(target: Long, duration: Long) {
    Box(Modifier.fillMaxSize()) {
        BasicText(
            "${clock(target / 1000.0)} / ${clock(duration / 1000.0)}",
            Modifier.align(Alignment.TopCenter).padding(top = 24.dp).background(Color(0xBB000000)).padding(horizontal = 18.dp, vertical = 8.dp),
            style = T.heading.copy(fontSize = 18.sp),
        )
        Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(4.dp).background(Color(0x33FFFFFF))) {
            Box(Modifier.fillMaxWidth(if (duration > 0) (target.toFloat() / duration).coerceIn(0f, 1f) else 0f).height(4.dp).background(C.signal))
        }
    }
}

@Composable
private fun StatusChip(line: String, visible: Boolean) {
    Box(Modifier.fillMaxSize().padding(top = 24.dp, end = SafeH), contentAlignment = Alignment.TopEnd) {
        Row(Modifier.background(Color(0x88000000)).padding(horizontal = 10.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).background(if (visible) C.signal else C.dim))
            BasicText(if (visible) line else "弹幕已关闭", Modifier.padding(start = 8.dp), style = T.small.copy(color = C.muted), maxLines = 1)
        }
    }
}

@Composable
private fun Controls(ui: PlayerUi, actions: PlayerActions) {
    val play = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { play.requestFocus() } }
    Box(Modifier.fillMaxSize()) {
        Box(Modifier.fillMaxWidth().height(120.dp).background(Brush.verticalGradient(listOf(Color(0xCC000000), Color.Transparent))))
        Box(Modifier.align(Alignment.BottomStart).fillMaxWidth().height(230.dp).background(Brush.verticalGradient(listOf(Color.Transparent, Color(0xDD000000)))))
        Column(Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(start = SafeH, end = SafeH, bottom = 28.dp)) {
            BasicText(ui.show?.title ?: "", style = T.heading, maxLines = 1, overflow = com.fanyu.tv.ui.Ellipsis)
            BasicText("第 ${ui.episode} 集", Modifier.padding(top = 2.dp), style = T.meta)
            SeekBar(ui, actions)
            Row(Modifier.padding(top = 18.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                TvButton(if (ui.playing) "暂停" else "播放", actions::togglePlay, icon = if (ui.playing) Glyph.Pause else Glyph.Play, requester = play)
                ui.next?.let { TvButton("第 ${it.n} 集", actions::playNext, icon = Glyph.Next) }
                TvButton("弹幕", { ui.overlay = Overlay.Danmu }, icon = Glyph.Danmu)
                TvButton("字幕", { ui.overlay = Overlay.Subtitles }, icon = Glyph.Subtitle)
                if (ui.current?.s != "watched") TvButton("标记看完", { actions.markWatched {} }, icon = Glyph.Check)
            }
        }
    }
}

@Composable
private fun SeekBar(ui: PlayerUi, actions: PlayerActions) {
    val fraction = if (ui.duration > 0) (ui.position.toFloat() / ui.duration).coerceIn(0f, 1f) else 0f
    Focusable(
        Modifier.padding(top = 16.dp).fillMaxWidth().onPreviewKeyEvent { e ->
            val k = e.nativeKeyEvent.keyCode
            if (k != AndroidKey.KEYCODE_DPAD_LEFT && k != AndroidKey.KEYCODE_DPAD_RIGHT) return@onPreviewKeyEvent false
            if (e.type == KeyEventType.KeyDown) actions.seekBy(if (k == AndroidKey.KEYCODE_DPAD_LEFT) -10_000 else 10_000)
            true
        },
        onClick = actions::togglePlay, scale = 1f,
    ) { focused ->
        Row(verticalAlignment = Alignment.CenterVertically) {
            BasicText(clock(ui.position / 1000.0), style = T.body.copy(fontWeight = FontWeight.SemiBold))
            var widthPx by remember { mutableStateOf(0) }
            val density = LocalDensity.current
            Box(Modifier.padding(horizontal = 14.dp).weight(1f).height(16.dp).onSizeChanged { widthPx = it.width }, contentAlignment = Alignment.CenterStart) {
                Box(Modifier.fillMaxWidth().height(if (focused) 8.dp else 5.dp).background(Color(0x44FFFFFF)))
                Box(Modifier.fillMaxWidth(fraction).height(if (focused) 8.dp else 5.dp).background(C.signal))
                if (focused) Box(Modifier.offset(x = with(density) { (widthPx * fraction).toDp() } - 8.dp).size(16.dp).background(C.ink))
            }
            BasicText(clock(ui.duration / 1000.0), style = T.body.copy(fontWeight = FontWeight.SemiBold))
        }
    }
}

@Composable
private fun Drawer(title: String, content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.CenterEnd) {
        Column(
            Modifier.width(340.dp).fillMaxHeight().background(C.bg).border(1.dp, C.line)
                .verticalScroll(rememberScrollState()).padding(horizontal = 28.dp, vertical = 30.dp)
        ) {
            BasicText(title, style = T.heading.copy(fontSize = 18.sp))
            content()
        }
    }
}

@Composable
private fun DrawerRow(
    label: String,
    value: String,
    requester: FocusRequester? = null,
    switch: Boolean? = null,
    onLeft: (() -> Unit)? = null,
    onRight: (() -> Unit)? = null,
    onClick: () -> Unit = { onRight?.invoke() },
) {
    Focusable(
        Modifier.fillMaxWidth().onPreviewKeyEvent { e ->
            val k = e.nativeKeyEvent.keyCode
            val handler = when (k) { AndroidKey.KEYCODE_DPAD_LEFT -> onLeft; AndroidKey.KEYCODE_DPAD_RIGHT -> onRight; else -> return@onPreviewKeyEvent false }
            if (e.type == KeyEventType.KeyDown) handler?.invoke()
            true
        },
        onClick = onClick, requester = requester, scale = 1f,
    ) { focused ->
        Row(
            Modifier.fillMaxWidth().height(48.dp).background(if (focused) C.layer2 else Color.Transparent)
                .border(if (focused) 3.dp else 0.dp, if (focused) C.signal else Color.Transparent).padding(horizontal = 14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BasicText(label, style = T.body)
            Spacer(Modifier.weight(1f))
            if (switch != null) {
                Box(Modifier.size(36.dp, 20.dp).background(if (switch) C.signal else C.line).padding(3.dp), contentAlignment = if (switch) Alignment.CenterEnd else Alignment.CenterStart) {
                    Box(Modifier.size(14.dp).background(if (switch) C.onSignal else C.muted))
                }
            } else if (onLeft != null) {
                val arrow = T.body.copy(color = if (focused) C.signal else C.dim, fontSize = 16.sp)
                BasicText("‹", style = arrow)
                BasicText(value, Modifier.padding(horizontal = 10.dp), style = T.body.copy(color = if (focused) C.ink else C.muted, fontWeight = FontWeight.SemiBold))
                BasicText("›", style = arrow)
            } else BasicText(value, style = T.body.copy(color = if (focused) C.ink else C.muted))
        }
    }
    Box(Modifier.fillMaxWidth().height(1.dp).background(C.line))
}

@Composable
private fun DanmuDrawer(ui: PlayerUi, actions: PlayerActions) {
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    val s = ui.settings
    fun set(v: DanmuSettings) = actions.updateSettings(v.clamped())
    fun <T> cycle(list: List<T>, i: Int, d: Int) = (i + d).coerceIn(list.indices)
    Drawer("弹幕") {
        Column(Modifier.padding(top = 6.dp, bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            if (ui.danmuSources.isEmpty()) BasicText(ui.danmuLine, style = T.small.copy(color = C.muted))
            else {
                BasicText("%,d 条".format(ui.danmuCount), style = T.small.copy(color = C.signal, fontWeight = FontWeight.SemiBold))
                ui.danmuSources.forEach { src ->
                    val detail = when {
                        src.error != null -> "获取失败"
                        src.alignment.isNotEmpty() -> src.alignment
                        src.offset != 0.0 -> "偏移 ${Danmu.signed(src.offset)}s"
                        else -> ""
                    }
                    BasicText("${src.name} · ${src.comments.size} 条" + if (detail.isNotEmpty()) " · $detail" else "", style = T.small.copy(color = C.muted), maxLines = 1)
                }
            }
        }
        DrawerRow("显示弹幕", "", first, switch = s.visible, onLeft = { set(s.copy(visible = !s.visible)) }, onRight = { set(s.copy(visible = !s.visible)) }, onClick = { set(s.copy(visible = !s.visible)) })
        DrawerRow("时间偏移", Danmu.signed(ui.globalOffset) + " 秒", onLeft = { actions.shiftDanmu(-.5) }, onRight = { actions.shiftDanmu(.5) })
        DrawerRow("字号", DanmuSettings.SIZES[s.size].first, onLeft = { set(s.copy(size = cycle(DanmuSettings.SIZES, s.size, -1))) }, onRight = { set(s.copy(size = cycle(DanmuSettings.SIZES, s.size, 1))) })
        DrawerRow("不透明度", DanmuSettings.OPACITY[s.opacity].first, onLeft = { set(s.copy(opacity = cycle(DanmuSettings.OPACITY, s.opacity, -1))) }, onRight = { set(s.copy(opacity = cycle(DanmuSettings.OPACITY, s.opacity, 1))) })
        DrawerRow("显示区域", DanmuSettings.AREAS[s.area].first, onLeft = { set(s.copy(area = cycle(DanmuSettings.AREAS, s.area, -1))) }, onRight = { set(s.copy(area = cycle(DanmuSettings.AREAS, s.area, 1))) })
        DrawerRow("速度", DanmuSettings.SPEEDS[s.speed].first, onLeft = { set(s.copy(speed = cycle(DanmuSettings.SPEEDS, s.speed, -1))) }, onRight = { set(s.copy(speed = cycle(DanmuSettings.SPEEDS, s.speed, 1))) })
        DrawerRow("重新匹配", "", onClick = actions::reloadDanmu)
    }
}

@Composable
private fun SubtitleDrawer(ui: PlayerUi, actions: PlayerActions) {
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    Drawer("字幕") {
        Spacer(Modifier.height(12.dp))
        val anySelected = !ui.subtitlesOff && ui.textTracks.any { it.selected }
        DrawerRow("关闭", if (!anySelected) "✓" else "", first, onClick = { actions.selectSubtitle(null) })
        ui.textTracks.forEach { t ->
            DrawerRow(t.label, if (anySelected && t.selected) "✓" else "", onClick = { actions.selectSubtitle(t) })
        }
        if (ui.textTracks.isEmpty()) BasicText("这个文件没有内嵌字幕", Modifier.padding(top = 12.dp), style = T.small)
    }
}

@Composable
private fun EndPanel(ui: PlayerUi, actions: PlayerActions) {
    val primary = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { primary.requestFocus() } }
    val next = ui.next
    val watched = ui.current?.s == "watched"
    Box(Modifier.fillMaxSize().background(Color(0xB0000000)), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            BasicText("第 ${ui.episode} 集放完了", style = T.title)
            Row(Modifier.padding(top = 22.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                if (watched) {
                    if (next != null) TvButton("播放第 ${next.n} 集", actions::playNext, icon = Glyph.Next, primary = true, requester = primary)
                    TvButton("返回", actions::close, requester = if (next == null) primary else null)
                } else {
                    if (next != null) TvButton("看完，播放第 ${next.n} 集", { actions.markWatched { actions.playNext() } }, icon = Glyph.Check, primary = true, requester = primary)
                    TvButton("标记看完", { actions.markWatched { actions.close() } }, icon = Glyph.Check, primary = next == null, requester = if (next == null) primary else null)
                    TvButton("不标记", actions::close)
                }
            }
        }
    }
}

@Composable
private fun ErrorPanel(message: String, actions: PlayerActions) {
    val primary = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { primary.requestFocus() } }
    Box(Modifier.fillMaxSize().background(Color(0xE6000000)), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            BasicText(message, style = T.heading)
            TvButton("返回", actions::close, Modifier.padding(top = 22.dp), primary = true, requester = primary)
        }
    }
}
