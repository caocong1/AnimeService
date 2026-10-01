package com.fanyu.tv

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.unit.dp
import com.fanyu.tv.ui.Brand
import com.fanyu.tv.ui.C
import com.fanyu.tv.ui.ConnectScreen
import com.fanyu.tv.ui.Focusable
import com.fanyu.tv.ui.SafeH
import com.fanyu.tv.ui.T
import com.fanyu.tv.ui.Toast
import com.fanyu.tv.ui.ToastState
import com.fanyu.tv.update.Release
import com.fanyu.tv.update.UpdateDialog
import com.fanyu.tv.update.Updater
import kotlinx.coroutines.launch

class SettingsActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { Screen() }
    }

    @Composable
    private fun Screen() {
        val scope = rememberCoroutineScope()
        val toast = remember { ToastState() }
        var connecting by remember { mutableStateOf(false) }
        var checking by remember { mutableStateOf(false) }
        var release by remember { mutableStateOf<Release?>(null) }
        val first = remember { FocusRequester() }
        if (connecting) {
            ConnectScreen(app.api, app.prefs, startPairing = false, onConnected = { connecting = false; toast.show("已连接") }, onCancel = { connecting = false })
            return
        }
        LaunchedEffect(Unit) { runCatching { first.requestFocus() } }
        Box(Modifier.fillMaxSize().background(C.bg)) {
            Column(Modifier.padding(horizontal = SafeH, vertical = 22.dp)) {
                Box(Modifier.height(36.dp), contentAlignment = Alignment.CenterStart) { Brand() }
                BasicText("设置", Modifier.padding(top = 18.dp, bottom = 14.dp), style = T.title)
                Column(Modifier.width(560.dp)) {
                    Item("服务器", app.prefs.server, first) { connecting = true }
                    Item("检查更新", if (checking) "检查中…" else "当前 ${BuildConfig.VERSION_NAME}") {
                        if (checking) return@Item
                        checking = true
                        scope.launch {
                            release = Updater.check(app.api)
                            if (release == null) toast.show("已是最新版本 ${BuildConfig.VERSION_NAME}")
                            checking = false
                        }
                    }
                }
            }
        }
        release?.let { UpdateDialog(it) { release = null } }
        Toast(toast)
    }
}

@Composable
private fun Item(label: String, value: String, requester: FocusRequester? = null, onClick: () -> Unit) {
    Focusable(Modifier.fillMaxWidth(), onClick = onClick, requester = requester, scale = 1.02f) { focused ->
        Row(
            Modifier.fillMaxWidth().height(56.dp).background(if (focused) C.layer2 else C.layer1)
                .border(if (focused) 3.dp else 0.dp, if (focused) C.signal else C.layer1).padding(horizontal = 18.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BasicText(label, style = T.body)
            Spacer(Modifier.weight(1f))
            BasicText(value, style = T.meta.copy(color = if (focused) C.ink else C.muted))
        }
    }
    Spacer(Modifier.height(2.dp))
}
