package com.fanyu.tv

import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.blur
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.fanyu.tv.data.ApiError
import com.fanyu.tv.data.Ep
import com.fanyu.tv.data.Show
import com.fanyu.tv.data.clock
import com.fanyu.tv.data.parseShowDetail
import com.fanyu.tv.player.PlayerActivity
import com.fanyu.tv.ui.C
import com.fanyu.tv.ui.Cover
import com.fanyu.tv.ui.Ellipsis
import com.fanyu.tv.ui.Focusable
import com.fanyu.tv.ui.KeyHints
import com.fanyu.tv.ui.SafeH
import com.fanyu.tv.ui.T
import com.fanyu.tv.ui.Tag
import com.fanyu.tv.ui.Toast
import com.fanyu.tv.ui.ToastState
import com.fanyu.tv.ui.dashedBorder
import kotlinx.coroutines.launch
import org.json.JSONObject

class ShowActivity : ComponentActivity() {
    private var resumes by mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent.getIntExtra("show", 0)
        setContent { Screen(id) }
    }

    override fun onResume() {
        super.onResume()
        resumes++
    }

    @Composable
    private fun Screen(id: Int) {
        val api = app.api
        val scope = rememberCoroutineScope()
        val toast = remember { ToastState() }
        var show by remember { mutableStateOf<Show?>(null) }
        var error by remember { mutableStateOf<ApiError?>(null) }
        var reloads by remember { mutableIntStateOf(0) }
        LaunchedEffect(resumes, reloads) {
            try { show = parseShowDetail(api.getObject("/api/shows/$id")); error = null } catch (e: ApiError) { error = e }
        }
        fun toggle(ep: Ep) {
            val finished = ep.s != "watched"
            scope.launch {
                try {
                    api.post("/api/shows/$id/watch", JSONObject().put("episode", ep.n).put("finished", finished))
                    toast.show(if (finished) "第 ${ep.n} 集已看" else "第 ${ep.n} 集改为未看"); reloads++
                } catch (e: ApiError) { toast.show(e.message ?: "未能保存") }
            }
        }
        val s = show
        when {
            s != null -> ShowPage(s, onPlay = { startActivity(PlayerActivity.intent(this, id, it.n)) }, onToggle = ::toggle, toast = toast)
            error != null -> ErrorPanel(error!!, retry = { error = null; reloads++ }, change = null)
            else -> Box(Modifier.fillMaxSize().background(C.bg))
        }
        Toast(toast)
    }
}

@Composable
private fun ShowPage(show: Show, onPlay: (Ep) -> Unit, onToggle: (Ep) -> Unit, toast: ToastState) {
    val ticketFocus = remember { FocusRequester() }
    val firstEp = remember { FocusRequester() }
    val nextEp = remember { FocusRequester() }
    // Down from the ticket lands on its own episode, not on whatever tile sits under it; only
    // while that tile is surely composed (the first two rows of the lazy grid).
    val nextIndex = show.eps.indexOfFirst { it.n == show.next?.n }
    val linkTicket = nextIndex in 1 until 20
    var focusedOnce by remember { mutableStateOf(false) }
    LaunchedEffect(show) {
        if (!focusedOnce) { focusedOnce = true; runCatching { if (show.next != null) ticketFocus.requestFocus() else firstEp.requestFocus() } }
    }
    Box(Modifier.fillMaxSize().background(C.bg)) {
        if (show.image != null) {
            val modern = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
            AsyncImage(show.image, null, Modifier.fillMaxSize().alpha(if (modern) .2f else .08f).let { if (modern) it.blur(48.dp) else it }, contentScale = ContentScale.Crop)
        }
        Column(Modifier.fillMaxSize().padding(top = 36.dp)) {
            Row(Modifier.padding(horizontal = SafeH)) {
                Cover(show.image, show.title, Modifier.size(150.dp, 212.dp))
                Column(Modifier.padding(start = 28.dp).fillMaxWidth()) {
                    BasicText(show.title, style = T.title, maxLines = 2, overflow = Ellipsis)
                    if (show.original.isNotEmpty() && show.original != show.title)
                        BasicText(show.original, Modifier.padding(top = 4.dp), style = T.small, maxLines = 1, overflow = Ellipsis)
                    Row(Modifier.padding(top = 10.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        if (show.stateLabel.isNotEmpty()) Tag(show.stateLabel)
                        BasicText("已看 ${show.watched}/${if (show.total > 0) show.total else "?"}", style = T.meta)
                        if (show.unwatched > 0) BasicText("待看 ${show.unwatched}", style = T.meta)
                    }
                    show.next?.let { Ticket(it, ticketFocus, if (linkTicket) nextEp else null, onPlay) }
                }
            }
            Row(Modifier.padding(start = SafeH, top = 22.dp), verticalAlignment = Alignment.Bottom) {
                BasicText("分集", style = T.section)
                BasicText("${show.eps.size}", Modifier.padding(start = 6.dp), style = T.small)
            }
            LazyVerticalGrid(
                GridCells.Fixed(10),
                Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(start = SafeH, end = SafeH, top = 12.dp, bottom = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                items(show.eps, key = { it.n }) { ep ->
                    EpTile(ep, if (ep === show.eps.first()) firstEp else if (linkTicket && ep.n == show.next?.n) nextEp else null,
                        onClick = { if (ep.playable) onPlay(ep) else toast.show(unplayable(ep)) },
                        onLongClick = { onToggle(ep) })
                }
            }
            KeyHints("OK" to "播放", "长按 OK" to "标记看完 / 未看", "返回" to "追番", modifier = Modifier.padding(start = SafeH, bottom = 16.dp))
        }
    }
}

private fun unplayable(ep: Ep) = when (ep.s) {
    "downloading" -> "第 ${ep.n} 集下载中 ${ep.progress}%"
    "missing" -> "第 ${ep.n} 集缺失"
    "future" -> "第 ${ep.n} 集还未播出"
    "watched" -> "第 ${ep.n} 集已看，文件不在片库"
    else -> "第 ${ep.n} 集没有可播放的文件"
}

@Composable
private fun Ticket(ep: Ep, requester: FocusRequester, below: FocusRequester?, onPlay: (Ep) -> Unit) {
    Focusable(Modifier.padding(top = 18.dp).let { m -> if (below != null) m.focusProperties { down = below } else m }, onClick = { onPlay(ep) }, requester = requester, scale = 1.05f) { focused ->
        Box(Modifier.let { if (focused) it.border(3.dp, C.ink).padding(4.dp) else it.padding(4.dp) }) {
            Row(Modifier.background(C.signal).height(72.dp).padding(horizontal = 22.dp), verticalAlignment = Alignment.CenterVertically) {
                BasicText("第", style = T.body.copy(color = C.onSignal, fontWeight = FontWeight.SemiBold))
                BasicText("${ep.n}", Modifier.padding(horizontal = 6.dp), style = TextStyle(color = C.onSignal, fontSize = 44.sp, fontWeight = FontWeight.Bold))
                BasicText("集", style = T.body.copy(color = C.onSignal, fontWeight = FontWeight.SemiBold))
                BasicText(
                    if (ep.s == "resume") "▶ 续播 ${clock(ep.position)}" else "▶ 播放",
                    Modifier.padding(start = 18.dp), style = T.body.copy(color = C.onSignal, fontWeight = FontWeight.SemiBold),
                )
            }
        }
    }
}

@Composable
private fun EpTile(ep: Ep, requester: FocusRequester?, onClick: () -> Unit, onLongClick: () -> Unit) {
    val (label, accent) = when (ep.s) {
        "watched" -> "已看" to C.dim
        "resume" -> clock(ep.position) to C.signal
        "ready" -> "可播放" to C.signal
        "downloading" -> "下载 ${ep.progress}%" to C.signal
        "missing" -> "缺" to C.warn
        "aired" -> "已播出" to C.dim
        else -> (ep.date?.takeLast(5) ?: "未播出") to C.dim
    }
    val border = when (ep.s) { "ready", "resume" -> C.signal; "missing" -> C.warn; "watched", "future", "aired" -> C.line; else -> C.lineStrong }
    Focusable(onClick = onClick, onLongClick = onLongClick, requester = requester, scale = 1.12f) { focused ->
        Box(
            Modifier.fillMaxWidth().height(52.dp)
                .background(if (focused) C.layer2 else if (ep.s == "resume") C.signalWash else Color.Transparent)
                .dashedBorder(if (focused) C.signal else border, dashed = !focused && ep.s in setOf("missing", "future", "aired"), width = if (focused) 3.dp else 1.dp),
            contentAlignment = Alignment.Center,
        ) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                BasicText("${ep.n}", style = TextStyle(color = if (ep.s == "watched" || ep.s == "future") C.dim else if (ep.s == "missing") C.warn else C.ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold, textAlign = TextAlign.Center))
                BasicText(label, style = T.label.copy(color = accent, fontSize = 11.sp), maxLines = 1)
            }
            if (ep.s == "resume" && ep.duration > 0) Box(Modifier.align(Alignment.BottomStart).fillMaxWidth((ep.position / ep.duration).toFloat().coerceIn(0f, 1f)).height(3.dp).background(C.signal))
        }
    }
}
