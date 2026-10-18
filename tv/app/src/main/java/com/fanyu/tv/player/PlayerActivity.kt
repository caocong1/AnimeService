package com.fanyu.tv.player

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.annotation.OptIn
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.lifecycleScope
import androidx.media3.common.C as MediaC
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.exoplayer.DefaultRenderersFactory
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.CaptionStyleCompat
import androidx.media3.ui.PlayerView
import com.fanyu.tv.app
import com.fanyu.tv.data.ApiError
import com.fanyu.tv.data.DanmuSettings
import com.fanyu.tv.data.Ep
import com.fanyu.tv.data.Show
import com.fanyu.tv.data.parseShowDetail
import com.fanyu.tv.ui.ToastState
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.Locale
import java.util.UUID

enum class Overlay { None, Controls, Danmu, Subtitles, End, Error }

data class TextTrack(val group: Tracks.Group, val index: Int, val label: String, val selected: Boolean)

/** Everything the overlay draws; the activity owns the player and mutates this. */
class PlayerUi {
    var overlay by mutableStateOf(Overlay.None)
    var show by mutableStateOf<Show?>(null)
    var episode by mutableIntStateOf(0)
    var playing by mutableStateOf(false)
    var buffering by mutableStateOf(true)
    var position by mutableLongStateOf(0L)
    var duration by mutableLongStateOf(0L)
    var seekPreview by mutableStateOf<Long?>(null)
    var error by mutableStateOf("")
    var danmuLine by mutableStateOf("弹幕匹配中…")
    var danmuSources by mutableStateOf<List<DanmuSource>>(emptyList())
    var danmuCount by mutableIntStateOf(0)
    var settings by mutableStateOf(DanmuSettings())
    var globalOffset by mutableStateOf(0.0)
    var textTracks by mutableStateOf<List<TextTrack>>(emptyList())
    var subtitlesOff by mutableStateOf(false)
    var interaction by mutableIntStateOf(0)
    var statusUntil by mutableLongStateOf(0L)
    /** Persistent progress-writer state: who holds progress / why nothing is saved, and the explicit action offered. */
    var progressStatus by mutableStateOf("")
    var progressTakeover by mutableStateOf<String?>(null)
    val toast = ToastState()

    val current: Ep? get() = show?.eps?.firstOrNull { it.n == episode }
    val next: Ep? get() = show?.after(episode)
}

@OptIn(UnstableApi::class)
class PlayerActivity : ComponentActivity() {
    companion object {
        /** [explicit] marks a one-shot user Play intent from the list; a recreated activity never re-consumes it. */
        fun intent(context: Context, show: Int, episode: Int, explicit: Boolean = false): Intent =
            Intent(context, PlayerActivity::class.java).putExtra("show", show).putExtra("episode", episode).putExtra("explicit_play", explicit)
    }

    private val ui = PlayerUi()
    private lateinit var player: ExoPlayer
    private lateinit var danmaku: DanmakuView
    private lateinit var playerView: PlayerView
    private lateinit var bufferPolicy: AdaptiveBufferPolicy
    private var showId = 0
    private var media: String? = null
    private var played = false
    private var lastSaved = 0.0
    private var seekKeyDown = false
    private var okDown = false
    private var danmuJob: Job? = null
    private var subtitleChosen = false
    private var writer: PlaybackWriterClient? = null
    private var writerIntentAt = Long.MIN_VALUE / 2
    private var writerClaiming = false
    private val writerSession = UUID.randomUUID().toString()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        showId = intent.getIntExtra("show", 0)
        ui.episode = intent.getIntExtra("episode", 0)
        ui.settings = app.prefs.danmu
        if (savedInstanceState == null && intent.getBooleanExtra("explicit_play", false)) writerIntent()

        val renderers = DefaultRenderersFactory(this).setEnableDecoderFallback(true)
        val maxHeapBytes = Runtime.getRuntime().maxMemory()
        bufferPolicy = AdaptiveBufferPolicy(maxHeapBytes)
        val transfers = PlaybackTransferEstimator { bytes, elapsed, at -> bufferPolicy.recordTransfer(bytes, elapsed, at) }
        val videoDataSource = OkHttpDataSource.Factory(app.api.client).setTransferListener(object : TransferListener {
            override fun onTransferInitializing(source: DataSource, dataSpec: DataSpec, isNetwork: Boolean) = Unit
            override fun onTransferStart(source: DataSource, dataSpec: DataSpec, isNetwork: Boolean) {
                if (isNetwork) transfers.transferStarted(SystemClock.elapsedRealtime())
            }
            override fun onBytesTransferred(source: DataSource, dataSpec: DataSpec, isNetwork: Boolean, bytesTransferred: Int) {
                if (isNetwork) transfers.bytesTransferred(bytesTransferred, SystemClock.elapsedRealtime())
            }
            override fun onTransferEnd(source: DataSource, dataSpec: DataSpec, isNetwork: Boolean) {
                if (isNetwork) transfers.transferEnded(SystemClock.elapsedRealtime())
            }
        })
        Log.i("FanyuPlayback", "heapLimitBytes=$maxHeapBytes bufferTargetBytes=${bufferPolicy.hardTargetBytes}")
        player = ExoPlayer.Builder(this, renderers)
            .setLoadControl(AdaptiveLoadControl(bufferPolicy, transfers))
            .setMediaSourceFactory(DefaultMediaSourceFactory(videoDataSource))
            .build()
        player.trackSelectionParameters = player.trackSelectionParameters.buildUpon()
            .setPreferredTextLanguages("zh", "chi", "zho")
            .setSelectUndeterminedTextLanguage(true)
            .setPreferredAudioLanguages("ja", "jpn")
            .build()
        player.addListener(listener)

        playerView = PlayerView(this).apply {
            useController = false
            isFocusable = false
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            setKeepContentOnPlayerReset(true)
            subtitleView?.setApplyEmbeddedStyles(true)
            subtitleView?.setStyle(CaptionStyleCompat(
                android.graphics.Color.WHITE, android.graphics.Color.TRANSPARENT, android.graphics.Color.TRANSPARENT,
                CaptionStyleCompat.EDGE_TYPE_OUTLINE, android.graphics.Color.BLACK, null,
            ))
            player = this@PlayerActivity.player
        }
        danmaku = DanmakuView(this).apply {
            clock = { player.currentPosition }
            playing = { player.isPlaying }
            settings = ui.settings
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when (ui.overlay) {
                    Overlay.None, Overlay.End, Overlay.Error -> finish()
                    Overlay.Danmu, Overlay.Subtitles -> ui.overlay = Overlay.Controls
                    Overlay.Controls -> ui.overlay = Overlay.None
                }
            }
        })

        setContent { PlayerScreen(ui, playerView, danmaku, actions) }
        load()
        lifecycleScope.launch {
            var ticks = 0
            var lastBufferSettings: AdaptiveBufferPolicy.Settings? = null
            while (true) {
                ui.position = player.currentPosition.coerceAtLeast(0)
                ui.duration = player.duration.takeIf { it != MediaC.TIME_UNSET } ?: 0L
                bufferPolicy.setMediaDuration(ui.duration)
                val runtime = Runtime.getRuntime()
                val bufferSettings = bufferPolicy.settings(SystemClock.elapsedRealtime(),
                    runtime.totalMemory() - runtime.freeMemory(), player.playbackParameters.speed)
                if (bufferSettings != lastBufferSettings) {
                    Log.i("FanyuPlayback", "bufferMode=${bufferSettings.mode} bufferTargetBytes=${bufferSettings.targetBytes}" +
                        " startMs=${bufferSettings.window.startMs} resumeMs=${bufferSettings.window.resumeMs}")
                    lastBufferSettings = bufferSettings
                }
                delay(500)
                if (++ticks % 30 == 0 && player.isPlaying) saveProgress()
            }
        }
    }

    private fun load() = lifecycleScope.launch {
        try {
            val show = parseShowDetail(app.api.getObject("/api/shows/$showId"))
            ui.show = show
            val ep = show.eps.firstOrNull { it.n == ui.episode }
            val id = ep?.media ?: run { fail("第 ${ui.episode} 集没有可播放的文件"); return@launch }
            media = id
            openWriter(id)
            ui.globalOffset = app.prefs.globalOffset(id)
            danmaku.offsetMillis = (ui.globalOffset * 1000).toLong()
            val item = app.api.getObject("/api/web/media/$id")
            bufferPolicy.setMediaSize(item.optLong("size", 0))
            val progress = item.optJSONObject("progress")
            val pos = progress?.optDouble("position", 0.0) ?: 0.0
            val dur = progress?.optDouble("duration", 0.0) ?: 0.0
            player.setMediaItem(MediaItem.fromUri(app.api.url("/api/web/media/$id/stream")))
            if (pos > 5 && pos < dur - 30) {
                player.seekTo((pos * 1000).toLong())
                lastSaved = pos
                ui.toast.show("从 ${com.fanyu.tv.data.clock(pos)} 继续")
            }
            player.prepare()
            player.playWhenReady = true
            loadDanmu()
        } catch (e: ApiError) {
            fail(if (e.needsPairing) "这台盒子的登录已失效，返回后重新配对" else e.message ?: "加载失败")
        }
    }

    /* ---------- explicit playback writer (v2), only when the bootstrap advertised it ---------- */

    private fun openWriter(id: String) {
        if (!PlaybackWriterClient.negotiated(app.api.negotiatedCapabilities)) return   // legacy progress stays untouched
        val label = PlaybackWriterClient.sanitizeLabel(app.prefs.writerLabel)
            ?: PlaybackWriterClient.deviceLabel("电视播放器").also { app.prefs.writerLabel = it }
        val client = PlaybackWriterClient(id, writerSession, label, WriterTransport(app.api))
        client.onChange { renderWriter() }
        writer = client
        renderWriter()
        lifecycleScope.launch {
            try { client.state() } catch (e: Exception) { ui.progressStatus = "进度未保存：连接失败" }
            renderWriter()
        }
    }

    private fun renderWriter() {
        val w = writer ?: run { ui.progressStatus = ""; ui.progressTakeover = null; return }
        val owner = w.conflict?.device ?: "另一设备"
        ui.progressStatus = when (w.status) {
            PlaybackWriterClient.Status.WRITING -> if (w.unsaved) "进度未保存：连接失败，播放继续" else ""
            PlaybackWriterClient.Status.CONFLICT, PlaybackWriterClient.Status.FENCED, PlaybackWriterClient.Status.STALE -> "由 $owner 持有进度 · 本机未保存"
            PlaybackWriterClient.Status.EXPIRED -> "进度未保存：会话已过期"
            PlaybackWriterClient.Status.ERROR, PlaybackWriterClient.Status.UNSUPPORTED -> "进度未保存：连接失败"
            PlaybackWriterClient.Status.IDLE, PlaybackWriterClient.Status.RELEASED -> "进度未保存：按播放开始记录"
            PlaybackWriterClient.Status.DISPOSED -> ""
        }
        ui.progressTakeover = when (w.status) {
            PlaybackWriterClient.Status.CONFLICT, PlaybackWriterClient.Status.FENCED -> "接管进度"
            PlaybackWriterClient.Status.STALE, PlaybackWriterClient.Status.EXPIRED -> "重新获取进度"
            else -> null
        }
    }

    /** open on an explicit Play/Resume; takeover/reacquire only from the visible button with the generation shown. */
    private fun claimWriter(mode: PlaybackWriterClient.Mode, expected: Long?) {
        val w = writer ?: return
        if (writerClaiming) return
        writerClaiming = true
        lifecycleScope.launch {
            try {
                val r = w.claim(mode, expected)
                renderWriter()
                if (r.ok) saveProgress()
            } catch (e: Exception) {
                ui.progressStatus = "进度未保存：连接失败"
            } finally { writerClaiming = false }
        }
    }

    private fun writerIntent() { writerIntentAt = SystemClock.elapsedRealtime() }
    private fun explicitIntentRecent() = SystemClock.elapsedRealtime() - writerIntentAt < 1500

    private fun loadDanmu() {
        val id = media ?: return
        danmuJob?.cancel()
        danmuJob = lifecycleScope.launch {
            ui.danmuLine = "弹幕匹配中…"
            val result = try { Danmu.load(app.api, app.prefs, id) } catch (e: ApiError) {
                ui.danmuLine = if (e.code == 0) "弹幕未连接" else "弹幕匹配失败"; ui.statusUntil = System.currentTimeMillis() + 6000; return@launch
            }
            if (result.sources.isEmpty()) {
                ui.danmuLine = "未匹配到弹幕"; ui.danmuSources = emptyList(); danmaku.setItems(emptyList())
                ui.statusUntil = System.currentTimeMillis() + 6000
                return@launch
            }
            ui.danmuSources = result.sources
            applyDanmu()
            ui.statusUntil = System.currentTimeMillis() + 6000
            for (source in result.sources) {
                if (source.saved || !source.bilibili || source.comments.isEmpty()) continue
                val offset = Danmu.align(app.api, id, source) { status -> source.alignment = status; refreshDanmuLine() }
                if (offset != null) {
                    source.offset = offset
                    app.prefs.setSourceOffset(id, source.identity, offset)
                    applyDanmu()
                }
            }
        }
    }

    private fun applyDanmu() {
        val rows = Danmu.mix(ui.danmuSources)
        ui.danmuCount = rows.size
        danmaku.setItems(rows)
        refreshDanmuLine()
    }

    private fun refreshDanmuLine() {
        val sources = ui.danmuSources
        ui.danmuSources = sources.toList()
        ui.danmuLine = "弹幕 %,d".format(Locale.US, ui.danmuCount) + sources.joinToString("") { s ->
            " · " + s.name + when {
                s.error != null -> " 获取失败"
                s.alignment.isNotEmpty() -> " " + s.alignment
                s.offset != 0.0 -> " 偏移 ${Danmu.signed(s.offset)}s"
                else -> ""
            }
        }
    }

    private fun fail(message: String) {
        ui.error = message
        ui.overlay = Overlay.Error
    }

    private val listener = object : Player.Listener {
        override fun onIsPlayingChanged(isPlaying: Boolean) {
            ui.playing = isPlaying
            if (isPlaying) played = true
            // Autoplay never claims; a recent explicit Play/Resume does.
            if (isPlaying) writer?.let { if (it.status != PlaybackWriterClient.Status.WRITING && explicitIntentRecent()) claimWriter(PlaybackWriterClient.Mode.OPEN, null) }
            danmaku.wake()
        }

        override fun onPlaybackStateChanged(state: Int) {
            ui.buffering = state == Player.STATE_BUFFERING || state == Player.STATE_IDLE
            if (state == Player.STATE_ENDED) {
                saveProgress()
                ui.overlay = Overlay.End
            }
        }

        override fun onPositionDiscontinuity(old: Player.PositionInfo, new: Player.PositionInfo, reason: Int) = danmaku.wake()

        override fun onTracksChanged(tracks: Tracks) {
            val text = tracks.groups.filter { it.type == MediaC.TRACK_TYPE_TEXT }.flatMap { g ->
                (0 until g.length).filter { g.isTrackSupported(it) }.map { i ->
                    val f = g.getTrackFormat(i)
                    val label = f.label ?: f.language?.let { Locale.forLanguageTag(it).getDisplayLanguage(Locale.CHINA).ifEmpty { it } } ?: "字幕"
                    TextTrack(g, i, label, g.isTrackSelected(i))
                }
            }
            ui.textTracks = text
            // Always show subtitles when the file has them, even untagged ones, preferring simplified Chinese.
            if (!subtitleChosen && text.isNotEmpty()) {
                subtitleChosen = true
                if (text.none { it.selected }) {
                    val best = text.firstOrNull { Regex("简|chs|sc|hans", RegexOption.IGNORE_CASE).containsMatchIn(it.label) } ?: text.first()
                    actions.selectSubtitle(best)
                }
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            saveProgress()
            val runtime = Runtime.getRuntime()
            // Exception messages/stack traces can include private stream URLs or headers.
            Log.e("FanyuPlayback", "errorCode=${error.errorCode} causeType=${error.cause?.javaClass?.simpleName}" +
                " heapUsedBytes=${runtime.totalMemory() - runtime.freeMemory()} heapLimitBytes=${runtime.maxMemory()}")
            fail(when (error.errorCode) {
                in 4000..4999 -> "这台盒子无法解码这个视频（${error.errorCodeName}）"
                in 2000..2999 -> "视频传输中断，检查网络后重试"
                in 3000..3999 -> "无法解析这个文件（${error.errorCodeName}）"
                else -> "无法播放（${error.errorCodeName}）"
            })
        }
    }

    /** The overlay's buttons call back into the activity through this. */
    val actions = object : PlayerActions {
        override fun togglePlay() {
            writerIntent()   // a user action on the controls
            if (player.playbackState == Player.STATE_ENDED) player.seekTo(0)
            player.playWhenReady = !player.playWhenReady
            if (!player.playWhenReady) saveProgress()
        }

        override fun takeoverProgress() {
            val w = writer ?: return
            claimWriter(PlaybackWriterClient.Mode.TAKEOVER, w.conflict?.generation ?: w.generation)
        }

        override fun seekBy(millis: Long) {
            val d = player.duration.takeIf { it != MediaC.TIME_UNSET } ?: Long.MAX_VALUE
            player.seekTo((player.currentPosition + millis).coerceIn(0, d))
        }

        override fun playNext() {
            val next = ui.next ?: return
            saveProgress()
            startActivity(intent(this@PlayerActivity, showId, next.n))
            finish()
        }

        override fun markWatched(then: () -> Unit) {
            lifecycleScope.launch {
                try {
                    app.api.post("/api/shows/$showId/watch", JSONObject().put("episode", ui.episode).put("finished", true))
                    ui.toast.show("第 ${ui.episode} 集已看")
                    ui.show = ui.show?.let { s -> s.copy(eps = s.eps.map { if (it.n == ui.episode) it.copy(s = "watched") else it }) }
                    then()
                } catch (e: ApiError) { ui.toast.show(e.message ?: "未能标记") }
            }
        }

        override fun close() = finish()

        override fun updateSettings(settings: DanmuSettings) {
            ui.settings = settings
            danmaku.settings = settings
            app.prefs.danmu = settings
        }

        override fun shiftDanmu(seconds: Double) {
            val id = media ?: return
            ui.globalOffset = (Math.round((ui.globalOffset + seconds) * 10) / 10.0).coerceIn(-600.0, 600.0)
            app.prefs.setGlobalOffset(id, ui.globalOffset)
            danmaku.offsetMillis = (ui.globalOffset * 1000).toLong()
            danmaku.invalidate()
        }

        override fun reloadDanmu() = loadDanmu()

        override fun selectSubtitle(track: TextTrack?) {
            val b = player.trackSelectionParameters.buildUpon()
            if (track == null) b.setTrackTypeDisabled(MediaC.TRACK_TYPE_TEXT, true)
            else b.setTrackTypeDisabled(MediaC.TRACK_TYPE_TEXT, false).setOverrideForType(TrackSelectionOverride(track.group.mediaTrackGroup, track.index))
            player.trackSelectionParameters = b.build()
            ui.subtitlesOff = track == null
        }
    }

    /** Only real playback moves the stored position, like the web player; opening or seeking never marks anything watched. */
    private fun saveProgress() {
        val id = media ?: return
        if (!played) return
        val d = player.duration
        if (d == MediaC.TIME_UNSET || d <= 0) return
        val pos = player.currentPosition / 1000.0
        val delta = pos - lastSaved
        val playing = player.isPlaying && delta > 0 && delta < 35
        lastSaved = pos
        val duration = d / 1000.0
        val w = writer
        if (w != null) {   // v2 was negotiated: the legacy route is never used, even on failure
            if (w.status != PlaybackWriterClient.Status.WRITING) { renderWriter(); return }
            app.scope.launch {   // outlives the activity so the last packet still goes out; the client serializes the sequence
                try { w.progress(pos, duration, playing) } catch (e: Exception) { ui.progressStatus = "进度未保存：连接失败" }
                renderWriter()
            }
            return
        }
        val body = JSONObject().put("position", pos).put("duration", duration).put("playing", playing)
        app.scope.launch {
            try { app.api.post("/api/web/media/$id/progress", body); ui.progressStatus = "" }
            catch (e: ApiError) { ui.progressStatus = "进度未保存：连接失败" }   // visible, never swallowed
        }
    }

    // Activity's public event hook is intentional: TV key-up/hold handling must run
    // before child views. AndroidX core annotates its compatibility implementation
    // RestrictedApi; keep this exemption on the override rather than all TV lint.
    @SuppressLint("RestrictedApi")
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        ui.interaction++
        if (ui.overlay != Overlay.None) return super.dispatchKeyEvent(event)
        val down = event.action == KeyEvent.ACTION_DOWN
        when (event.keyCode) {
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER, KeyEvent.KEYCODE_SPACE,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> {
                if (down && event.repeatCount == 0) okDown = true
                if (!down && okDown) {
                    okDown = false
                    writerIntent()
                    actions.togglePlay()
                    if (!player.playWhenReady) ui.overlay = Overlay.Controls
                }
                return true
            }
            KeyEvent.KEYCODE_MEDIA_PLAY -> { if (down) { writerIntent(); player.play() }; return true }
            KeyEvent.KEYCODE_MEDIA_PAUSE -> { if (down) { player.pause(); saveProgress() }; return true }
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_MEDIA_REWIND, KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> {
                val sign = if (event.keyCode == KeyEvent.KEYCODE_DPAD_LEFT || event.keyCode == KeyEvent.KEYCODE_MEDIA_REWIND) -1 else 1
                if (down) {
                    if (event.repeatCount == 0) seekKeyDown = true
                    if (!seekKeyDown) return true
                    // Holding the key accelerates: 10 s, then 20 s, then 30 s per step.
                    val step = when { event.repeatCount >= 12 -> 30_000L; event.repeatCount >= 4 -> 20_000L; else -> 10_000L }
                    val d = player.duration.takeIf { it != MediaC.TIME_UNSET } ?: Long.MAX_VALUE
                    ui.seekPreview = ((ui.seekPreview ?: player.currentPosition) + sign * step).coerceIn(0, d)
                } else if (seekKeyDown) {
                    seekKeyDown = false
                    ui.seekPreview?.let { player.seekTo(it) }
                    lifecycleScope.launch { delay(900); if (!seekKeyDown) ui.seekPreview = null }
                }
                return true
            }
            KeyEvent.KEYCODE_DPAD_UP -> { if (!down) ui.overlay = Overlay.Danmu; return true }
            KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_INFO -> { if (!down) ui.overlay = Overlay.Controls; return true }
            KeyEvent.KEYCODE_MEDIA_NEXT -> { if (!down) actions.playNext(); return true }
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onStop() {
        super.onStop()
        saveProgress()
        player.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        writer?.dispose()   // late answers become inert; the final progress already left through app.scope
        danmuJob?.cancel()
        playerView.player = null
        player.removeListener(listener)
        player.release()
    }
}

interface PlayerActions {
    fun togglePlay()
    fun takeoverProgress()
    fun seekBy(millis: Long)
    fun playNext()
    fun markWatched(then: () -> Unit)
    fun close()
    fun updateSettings(settings: DanmuSettings)
    fun shiftDanmu(seconds: Double)
    fun reloadDanmu()
    fun selectSubtitle(track: TextTrack?)
}
