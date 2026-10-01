package com.fanyu.tv.ui

import android.os.Build
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
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
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.blur
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.fanyu.tv.data.Ep
import com.fanyu.tv.data.Show
import com.fanyu.tv.data.clock
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun HomeScreen(
    shows: List<Show>,
    onPlay: (Show, Ep) -> Unit,
    onOpen: (Show) -> Unit,
    onSettings: () -> Unit,
    onRefresh: () -> Unit,
) {
    // Something to resume comes first; otherwise the server's order (newest download first).
    val ready = remember(shows) { shows.filter { it.next != null }.sortedBy { if (it.next?.s == "resume") 0 else 1 } }
    var hero by remember { mutableStateOf<Show?>(null) }
    var hasFocus by remember { mutableStateOf(false) }
    val first = remember { FocusRequester() }
    val firstAll = remember { FocusRequester() }

    // After a reload removed the focused card (an episode got watched), focus goes to the first card.
    LaunchedEffect(shows) {
        delay(80)
        if (!hasFocus) runCatching { (if (ready.isNotEmpty()) first else firstAll).requestFocus() }
    }
    val shown = hero?.let { h -> shows.firstOrNull { it.id == h.id } } ?: ready.firstOrNull() ?: shows.firstOrNull()

    Box(Modifier.fillMaxSize().background(C.bg).onFocusChanged { hasFocus = it.hasFocus }) {
        shown?.image?.let { BackdropArt(it) }
        Column(Modifier.fillMaxSize()) {
            TopBar(onSettings)
            if (shows.isEmpty()) {
                EmptyHome(onRefresh, firstAll)
                return@Column
            }
            Hero(shown, Modifier.padding(horizontal = SafeH).height(150.dp))
            Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())) {
                if (ready.isNotEmpty()) PosterRow("可以看", ready, first, onFocus = { hero = it }, onClick = { s -> s.next?.let { onPlay(s, it) } }, onLongClick = onOpen, ticket = ::readyTicket)
                PosterRow("全部追番", shows, firstAll, onFocus = { hero = it }, onClick = onOpen, onLongClick = onOpen, ticket = ::allTicket)
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

@Composable
private fun BackdropArt(image: String) {
    val modern = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
    AsyncImage(
        image, null,
        Modifier.fillMaxWidth().height(300.dp).alpha(if (modern) .3f else .12f).let { if (modern) it.blur(40.dp) else it },
        contentScale = ContentScale.Crop,
    )
    Box(Modifier.fillMaxWidth().height(300.dp).background(androidx.compose.ui.graphics.Brush.verticalGradient(listOf(Color.Transparent, C.bg))))
}

@Composable
private fun TopBar(onSettings: () -> Unit) {
    var now by remember { mutableStateOf(Date()) }
    LaunchedEffect(Unit) { while (true) { now = Date(); delay(20_000) } }
    Row(Modifier.fillMaxWidth().padding(start = SafeH, end = SafeH - 6.dp, top = 22.dp).height(36.dp), verticalAlignment = Alignment.CenterVertically) {
        Brand()
        Spacer(Modifier.weight(1f))
        BasicText(SimpleDateFormat("HH:mm", Locale.CHINA).format(now), style = T.heading.copy(color = C.muted, fontSize = 18.sp))
        Spacer(Modifier.width(16.dp))
        Focusable(onClick = onSettings, scale = 1.12f) { focused ->
            Box(Modifier.size(36.dp).background(if (focused) C.ink else Color.Transparent), contentAlignment = Alignment.Center) {
                Icon(Glyph.Gear, if (focused) Color(0xFF111111) else C.muted, 20.dp)
            }
        }
    }
}

@Composable
fun Brand() {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(24.dp).background(Color(0xFF171A18)), contentAlignment = Alignment.Center) {
            BasicText("A", style = T.section.copy(color = Color(0xFFF2C66D), fontWeight = FontWeight.Bold))
        }
        BasicText("番屿", Modifier.padding(start = 8.dp), style = T.heading.copy(fontSize = 18.sp))
    }
}

@Composable
private fun Hero(show: Show?, modifier: Modifier) {
    Column(modifier.fillMaxWidth().padding(top = 14.dp)) {
        if (show == null) return@Column
        BasicText(show.title, Modifier.fillMaxWidth(.78f), style = T.title, maxLines = 1, overflow = Ellipsis)
        Row(Modifier.padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            if (show.stateLabel.isNotEmpty()) Tag(show.stateLabel)
            BasicText("${show.watched}/${if (show.total > 0) show.total else "?"}", style = T.meta)
            if (show.unwatched > 0) BasicText("待看 ${show.unwatched}", style = T.meta)
        }
        val next = show.next
        if (next?.s == "resume" && next.duration > 0) {
            ProgressLine((next.position / next.duration).toFloat(), Modifier.padding(top = 12.dp).width(240.dp))
        }
        BasicText(heroLine(show), Modifier.padding(top = 8.dp), style = T.meta.copy(fontSize = 13.sp))
        val from = next?.n ?: show.eps.lastOrNull { it.s == "watched" }?.n ?: 0
        val later = show.eps.filter { it.n > from }.take(7)
        if (later.isNotEmpty()) Row(Modifier.padding(top = 10.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            BasicText("之后", style = T.small)
            later.forEach { EpChip(it) }
        }
    }
}

private fun heroLine(show: Show): String {
    val next = show.next
    if (next != null) return if (next.s == "resume") "第 ${next.n} 集 · 看到 ${clock(next.position)} / ${clock(next.duration)}" else "第 ${next.n} 集 · 可播放"
    show.eps.firstOrNull { it.s == "downloading" }?.let { return "第 ${it.n} 集 · 下载中 ${it.progress}%" }
    show.eps.firstOrNull { it.s == "future" }?.let { return "第 ${it.n} 集" + (it.date?.let { d -> " · ${d.takeLast(5)} 播出" } ?: " · 未播出") }
    return if (show.eps.isNotEmpty() && show.eps.all { it.s == "watched" }) "已全部看完" else "暂无可播放的集"
}

private fun readyTicket(show: Show): Pair<String, Boolean> {
    val next = show.next ?: return "" to false
    return (if (next.s == "resume") "▶ 第${next.n}集 续播" else "第${next.n}集") to (next.s == "resume")
}

private fun allTicket(show: Show): Pair<String, Boolean> =
    (if (show.unwatched > 0) "待看 ${show.unwatched}" else show.stateLabel) to false

@Composable
private fun PosterRow(
    label: String,
    shows: List<Show>,
    first: FocusRequester,
    onFocus: (Show) -> Unit,
    onClick: (Show) -> Unit,
    onLongClick: (Show) -> Unit,
    ticket: (Show) -> Pair<String, Boolean>,
) {
    Row(Modifier.padding(start = SafeH, top = 10.dp), verticalAlignment = Alignment.Bottom) {
        BasicText(label, style = T.section)
        BasicText("${shows.size}", Modifier.padding(start = 6.dp), style = T.small)
    }
    LazyRow(
        contentPadding = PaddingValues(horizontal = SafeH, vertical = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        items(shows, key = { it.id }) { show ->
            val (text, lit) = ticket(show)
            Focusable(
                Modifier.width(100.dp),
                onClick = { onClick(show) },
                onLongClick = { onLongClick(show) },
                onFocus = { onFocus(show) },
                requester = if (show === shows.first()) first else null,
                scale = 1.1f,
            ) { focused ->
                Column {
                    Box(Modifier.size(100.dp, 142.dp).let { if (focused) it.border(3.dp, C.signal) else it }.padding(if (focused) 3.dp else 0.dp)) {
                        Cover(show.image, show.title, Modifier.fillMaxSize())
                        if (text.isNotEmpty()) BasicText(
                            text,
                            Modifier.align(Alignment.BottomStart).background(if (lit || focused) C.signal else C.signalWash).padding(horizontal = 6.dp, vertical = 2.dp),
                            style = T.label.copy(color = if (lit || focused) C.onSignal else C.signal, fontWeight = FontWeight.SemiBold),
                            maxLines = 1,
                        )
                    }
                    BasicText(
                        show.title, Modifier.padding(top = 6.dp),
                        style = T.small.copy(color = if (focused) C.ink else C.muted, fontWeight = if (focused) FontWeight.SemiBold else FontWeight.Normal),
                        maxLines = 1, overflow = Ellipsis,
                    )
                }
            }
        }
    }
}

@Composable
private fun EmptyHome(onRefresh: () -> Unit, requester: FocusRequester) {
    Column(Modifier.fillMaxSize(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        BasicText("还没有追番", style = T.heading)
        BasicText("在网页端的新番目录里把作品设为想看、试看或在追", Modifier.padding(top = 8.dp, bottom = 20.dp), style = T.meta)
        TvButton("刷新", onRefresh, requester = requester)
    }
}
