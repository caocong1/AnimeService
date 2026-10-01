package com.fanyu.tv.update

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.fanyu.tv.BuildConfig
import com.fanyu.tv.app
import com.fanyu.tv.ui.C
import com.fanyu.tv.ui.ProgressLine
import com.fanyu.tv.ui.T
import com.fanyu.tv.ui.TvButton
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.File

@Composable
fun UpdateDialog(release: Release, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var progress by remember { mutableStateOf<Float?>(null) }
    var file by remember { mutableStateOf<File?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var job by remember { mutableStateOf<Job?>(null) }
    val primary = remember { FocusRequester() }

    fun start() {
        file?.let { Updater.install(context, it); return }
        if (job?.isActive == true) return
        error = null; progress = 0f
        job = scope.launch {
            try {
                val f = Updater.download(context, context.app.api, release) { progress = it }
                file = f
                Updater.install(context, f)
            } catch (e: Exception) {
                error = e.message ?: "下载失败"; progress = null
            }
        }
    }

    Dialog(onDismissRequest = { job?.cancel(); onDismiss() }) {
        LaunchedEffect(Unit) { runCatching { primary.requestFocus() } }
        Column(Modifier.width(460.dp).background(C.layer1).border(1.dp, C.line).padding(28.dp)) {
            BasicText("有新版本", style = T.heading)
            BasicText("${BuildConfig.VERSION_NAME} → ${release.name}", Modifier.padding(top = 4.dp), style = T.body.copy(color = C.signal, fontWeight = FontWeight.SemiBold))
            if (release.notes.isNotEmpty()) Column(Modifier.padding(top = 12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                release.notes.forEach { BasicText("· $it", style = T.meta) }
            }
            progress?.let { ProgressLine(it, Modifier.padding(top = 20.dp).fillMaxWidth(), height = 6.dp) }
            error?.let { BasicText(it, Modifier.padding(top = 14.dp), style = T.meta.copy(color = C.danger)) }
            Row(Modifier.padding(top = 22.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                val label = when {
                    file != null -> "安装"
                    progress != null -> "下载中 ${((progress ?: 0f) * 100).toInt()}%"
                    error != null -> "重试"
                    else -> "立即更新"
                }
                TvButton(label, ::start, primary = true, requester = primary)
                TvButton("稍后", { job?.cancel(); onDismiss() })
            }
        }
    }
}
