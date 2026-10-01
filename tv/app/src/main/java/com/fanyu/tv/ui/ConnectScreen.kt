package com.fanyu.tv.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
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
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalView
import android.view.inputmethod.InputMethodManager
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.fanyu.tv.data.Api
import com.fanyu.tv.data.ApiError
import com.fanyu.tv.data.Prefs
import kotlinx.coroutines.launch
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** First run, a changed server, or a remote server that needs a pairing code. */
@Composable
fun ConnectScreen(api: Api, prefs: Prefs, startPairing: Boolean, onConnected: () -> Unit, onCancel: (() -> Unit)?) {
    val scope = rememberCoroutineScope()
    val initial = prefs.server.ifEmpty { "https://" }
    var address by remember { mutableStateOf(TextFieldValue(initial, TextRange(initial.length))) }
    var code by remember { mutableStateOf(TextFieldValue("")) }
    var pairing by remember { mutableStateOf(startPairing) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<Pair<String, String>?>(null) }
    val fieldFocus = remember { FocusRequester() }
    val codeFocus = remember { FocusRequester() }
    val view = LocalView.current
    val focusManager = LocalFocusManager.current

    // A TV keyboard left "shown" keeps swallowing the D-pad on the next screen, so close it explicitly.
    fun hideKeyboard() = view.context.getSystemService(InputMethodManager::class.java)?.hideSoftInputFromWindow(view.windowToken, 0)
    fun done() {
        focusManager.clearFocus(force = true)
        hideKeyboard()
        onConnected()
    }

    fun explain(e: ApiError, url: String): Pair<String, String> {
        val u = url.toHttpUrlOrNull()
        val authority = u?.let { it.host + if (it.port == HttpUrl.defaultPort(it.scheme)) "" else ":" + it.port } ?: url
        val remote = u?.isHttps == true
        return when {
            e.code == ApiError.TLS -> "HTTPS 证书无法验证" to "先检查盒子的日期和时间是否正确；证书须由公共机构签发"
            e.code == 0 -> "连不上 $url" to
                if (remote) "确认地址和端口正确、家中电脑的番屿服务在运行、外网端口已映射" else "确认家中电脑的番屿服务在运行，且盒子与电脑在同一网络"
            e.untrusted -> "访问地址不受信任" to
                if (remote) "在家中电脑的 data/deployment.json 的 public_authorities 加入 $authority"
                else "在家中电脑的 data/deployment.json 设置 lan_authority 为 $authority，lan_network 包含这台盒子的网段"
            e.code == 401 && pairing -> "配对码无效或已过期" to "在家中电脑的番屿后台重新生成配对码"
            else -> (e.message ?: "连接失败") to ""
        }
    }

    fun connect() {
        if (busy) return
        val url = Api.normalize(address.text)
        if (url == null) { error = "地址格式不对" to "例如 https://anime.example.com:38443"; return }
        val previous = prefs.server
        prefs.server = url
        busy = true; error = null
        scope.launch {
            try {
                api.bootstrap(); done()
            } catch (e: ApiError) {
                if (e.code == 401) { pairing = true; error = null; runCatching { codeFocus.requestFocus() } }
                else { error = explain(e, url); hideKeyboard(); if (previous.isNotEmpty()) prefs.server = previous }
            } finally { busy = false }
        }
    }

    fun pair() {
        if (busy) return
        busy = true; error = null
        scope.launch {
            try { api.pair(code.text); done() }
            catch (e: ApiError) { error = explain(e, prefs.server); hideKeyboard() }
            finally { busy = false }
        }
    }

    LaunchedEffect(pairing) { runCatching { if (pairing) codeFocus.requestFocus() else fieldFocus.requestFocus() } }

    Box(Modifier.fillMaxSize().background(C.bg)) {
        Box(Modifier.padding(start = SafeH, top = 22.dp).height(36.dp), contentAlignment = Alignment.CenterStart) { Brand() }
        Column(Modifier.align(Alignment.Center).width(540.dp)) {
            BasicText(if (pairing) "输入配对码" else "连接番屿服务", style = T.title)
            if (!pairing) {
                Field(address, { address = it }, fieldFocus, KeyboardType.Uri, ::connect)
                BasicText("番屿的外网地址，如 https://anime.example.com:38443；局域网可直接填电脑 IP", Modifier.padding(top = 8.dp), style = T.small)
            } else {
                BasicText(prefs.server, Modifier.padding(top = 6.dp), style = T.meta)
                Field(code, { code = it.copy(text = it.text.uppercase().take(20)) }, codeFocus, KeyboardType.Ascii, ::pair)
                BasicText("在家中电脑的番屿后台生成，10 分钟内有效；配对后这台盒子一年内不用再配", Modifier.padding(top = 8.dp), style = T.small)
            }
            error?.let { (title, detail) ->
                Row(Modifier.padding(top = 18.dp).fillMaxWidth().background(C.layer1).height(IntrinsicSize.Min)) {
                    Box(Modifier.width(3.dp).fillMaxHeight().background(C.danger))
                    Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
                        BasicText(title, style = T.body.copy(color = C.danger, fontWeight = FontWeight.SemiBold))
                        if (detail.isNotEmpty()) BasicText(detail, style = T.meta)
                    }
                }
            }
            Row(Modifier.padding(top = 20.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                TvButton(if (busy) "连接中…" else if (pairing) "配对" else "连接", { if (pairing) pair() else connect() }, primary = true)
                if (pairing) TvButton("更换地址", { pairing = false; error = null })
                else if (onCancel != null) TvButton("取消", onCancel)
            }
        }
    }
}

@Composable
private fun Field(value: TextFieldValue, onChange: (TextFieldValue) -> Unit, requester: FocusRequester, type: KeyboardType, onGo: () -> Unit) {
    var focused by remember { mutableStateOf(false) }
    BasicTextField(
        value, onChange,
        Modifier.padding(top = 14.dp).fillMaxWidth().focusRequester(requester).onFocusChanged { focused = it.isFocused },
        singleLine = true,
        textStyle = T.heading.copy(fontSize = 20.sp),
        cursorBrush = SolidColor(C.signal),
        keyboardOptions = KeyboardOptions(keyboardType = type, imeAction = ImeAction.Go, autoCorrectEnabled = false),
        keyboardActions = KeyboardActions(onGo = { onGo() }),
        decorationBox = { inner ->
            Box(
                Modifier.fillMaxWidth().height(52.dp).background(C.layer1).border(2.dp, if (focused) C.signal else C.line).padding(horizontal = 16.dp),
                contentAlignment = Alignment.CenterStart,
            ) { inner() }
        },
    )
}
