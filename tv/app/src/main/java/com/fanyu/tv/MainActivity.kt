package com.fanyu.tv

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.unit.dp
import com.fanyu.tv.data.ApiError
import com.fanyu.tv.data.Ep
import com.fanyu.tv.data.Show
import com.fanyu.tv.data.parseShows
import com.fanyu.tv.player.PlayerActivity
import com.fanyu.tv.ui.C
import com.fanyu.tv.ui.ConnectScreen
import com.fanyu.tv.ui.HomeScreen
import com.fanyu.tv.ui.T
import com.fanyu.tv.ui.TvButton
import com.fanyu.tv.update.Release
import com.fanyu.tv.update.UpdateDialog
import com.fanyu.tv.update.Updater

class MainActivity : ComponentActivity() {
    private var resumes by mutableIntStateOf(0)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { Main() }
    }

    override fun onResume() {
        super.onResume()
        resumes++
    }

    @Composable
    private fun Main() {
        val api = app.api
        var connecting by remember { mutableStateOf(app.prefs.server.isEmpty()) }
        var pairing by remember { mutableStateOf(false) }
        var shows by remember { mutableStateOf<List<Show>?>(null) }
        var error by remember { mutableStateOf<ApiError?>(null) }
        var release by remember { mutableStateOf<Release?>(null) }
        var checked by remember { mutableStateOf(false) }
        var reloads by remember { mutableIntStateOf(0) }

        if (connecting) {
            ConnectScreen(api, app.prefs, pairing, onConnected = { connecting = false; pairing = false; shows = null; error = null; reloads++ }, onCancel = null)
            return
        }
        LaunchedEffect(resumes, reloads) {
            try {
                shows = parseShows(api.getArray("/api/shows?scope=home")); error = null
                if (!checked) { checked = true; release = Updater.check(api) }
            } catch (e: ApiError) {
                if (e.code == 401) { pairing = true; connecting = true } else error = e
            }
        }
        val list = shows
        when {
            list != null -> HomeScreen(list, onPlay = ::play, onOpen = ::open, onSettings = ::settings, onRefresh = { reloads++ })
            error != null -> ErrorPanel(error!!, retry = { error = null; reloads++ }, change = { connecting = true })
            else -> Box(Modifier.fillMaxSize().background(C.bg), contentAlignment = Alignment.Center) { BasicText("加载中…", style = T.meta) }
        }
        release?.let { UpdateDialog(it) { release = null } }
    }

    private fun play(show: Show, ep: Ep) = startActivity(PlayerActivity.intent(this, show.id, ep.n))
    private fun open(show: Show) = startActivity(Intent(this, ShowActivity::class.java).putExtra("show", show.id))
    private fun settings() = startActivity(Intent(this, SettingsActivity::class.java))
}

@Composable
fun ErrorPanel(error: ApiError, retry: () -> Unit, change: (() -> Unit)?) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val first = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
    Box(Modifier.fillMaxSize().background(C.bg), contentAlignment = Alignment.Center) {
        Column(Modifier.width(520.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            BasicText(error.message ?: "出错了", style = T.heading)
            val hint = when {
                error.code == ApiError.TLS -> "检查盒子的日期和时间是否正确"
                error.code == 0 -> "确认网络正常、家中电脑的番屿服务在运行"
                error.untrusted -> "在家中电脑的 data/deployment.json 配置访问地址"
                error.needsPairing -> "这台盒子的登录已失效，需要重新输入配对码"
                else -> ""
            }
            if (hint.isNotEmpty()) BasicText(hint, Modifier.padding(top = 8.dp), style = T.meta)
            Row(Modifier.padding(top = 22.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                // Pairing lives on the home screen; it re-checks the session when it comes back to front.
                if (error.needsPairing && context !is MainActivity) TvButton("重新配对", {
                    context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
                }, primary = true, requester = first)
                else TvButton("重试", retry, primary = true, requester = first)
                if (change != null) TvButton("更换服务器", change)
            }
        }
    }
}
