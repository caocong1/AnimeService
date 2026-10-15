package com.fanyu.tv.player

/** Per-playback measurements only. No addresses, credentials or device identifiers are retained. */
internal class AdaptiveBufferPolicy(private val maxHeapBytes: Long) {
    enum class Mode { UNKNOWN, FAST, NORMAL, CAUTIOUS }
    data class Window(val minMs: Int, val maxMs: Int, val startMs: Int, val resumeMs: Int)
    data class Settings(val mode: Mode, val window: Window, val targetBytes: Int)

    val hardTargetBytes = PlaybackBufferPolicy.targetBytes(maxHeapBytes)
    private var sizeBytes = 0L
    private var durationMs = 0L
    private var throughput = 0.0
    private var variation = 0.0
    private var samples = 0
    private var lastSampleMs = -1L
    private var lastStallMs = -1L
    private var mode = Mode.UNKNOWN
    private var candidate = Mode.UNKNOWN
    private var candidateSinceMs = 0L
    private var memoryTight = false
    private var memoryHealthySinceMs = -1L
    private var loading = false

    @Synchronized fun setMediaSize(bytes: Long) { sizeBytes = bytes.coerceAtLeast(0) }
    @Synchronized fun setMediaDuration(millis: Long) { durationMs = millis.coerceAtLeast(0) }

    @Synchronized fun recordTransfer(bytes: Long, elapsedMs: Long, nowMs: Long) {
        // Short index reads and tiny ranges are not representative throughput measurements.
        if (bytes < 64 * 1024 || elapsedMs < 100) return
        val rate = bytes.toDouble() * 8_000.0 / elapsedMs
        if (!rate.isFinite() || rate <= 0) return
        if (lastSampleMs < 0 || nowMs - lastSampleMs > 60_000) {
            throughput = rate
            variation = 0.0
            samples = 1
        } else {
            val difference = kotlin.math.abs(rate - throughput) / throughput.coerceAtLeast(1.0)
            variation = variation * 0.75 + difference.coerceAtMost(4.0) * 0.25
            throughput = throughput * 0.75 + rate * 0.25
            samples = (samples + 1).coerceAtMost(100)
        }
        lastSampleMs = nowMs
    }

    @Synchronized fun recordRebuffer(atMs: Long) {
        if (atMs >= 0 && atMs > lastStallMs) lastStallMs = atMs
    }

    @Synchronized fun settings(nowMs: Long, usedHeapBytes: Long, speed: Float = 1f): Settings {
        if (usedHeapBytes.toDouble() >= maxHeapBytes.toDouble() * 0.75) {
            memoryTight = true
            memoryHealthySinceMs = -1
        } else if (memoryTight) {
            if (usedHeapBytes.toDouble() < maxHeapBytes.toDouble() * 0.60) {
                if (memoryHealthySinceMs < 0) memoryHealthySinceMs = nowMs
                if (nowMs - memoryHealthySinceMs >= 30_000) memoryTight = false
            } else memoryHealthySinceMs = -1
        }
        val bitrate = if (sizeBytes > 0 && durationMs > 0) sizeBytes.toDouble() * 8_000 / durationMs else 0.0
        val ratio = if (bitrate > 0) throughput / (bitrate * safeSpeed(speed)) else 0.0
        val wanted = when {
            lastStallMs >= 0 && nowMs - lastStallMs < 120_000 -> Mode.CAUTIOUS
            samples < 3 || bitrate <= 0 || lastSampleMs < 0 || nowMs - lastSampleMs > 60_000 -> Mode.UNKNOWN
            ratio >= 3.0 && variation < 0.35 -> Mode.FAST
            ratio >= 1.5 && variation < 0.60 -> Mode.NORMAL
            else -> Mode.CAUTIOUS
        }
        if (wanted != candidate) { candidate = wanted; candidateSinceMs = nowMs }
        // More protection immediately; less waiting only after a stable observation period.
        if (wanted == Mode.CAUTIOUS || (wanted == Mode.UNKNOWN && mode != Mode.CAUTIOUS) ||
            (mode == Mode.FAST && wanted == Mode.NORMAL) || nowMs - candidateSinceMs >= 30_000) {
            mode = wanted
        }
        val window = when (mode) {
            Mode.UNKNOWN -> Window(10_000, 30_000, 2_500, 5_000)
            Mode.FAST -> Window(8_000, 15_000, 1_000, 3_000)
            Mode.NORMAL -> Window(10_000, 25_000, 2_500, 5_000)
            Mode.CAUTIOUS -> Window(15_000, 30_000, 4_000, 8_000)
        }
        val target = if (memoryTight) (hardTargetBytes / 2).coerceAtLeast(2 * 1024 * 1024) else hardTargetBytes
        return Settings(mode, window, target.coerceAtMost(hardTargetBytes))
    }

    @Synchronized fun shouldLoad(settings: Settings, bufferedUs: Long, allocatedBytes: Int, speed: Float): Boolean {
        val playoutMs = bufferedUs.coerceAtLeast(0) / (1_000.0 * safeSpeed(speed))
        loading = when {
            allocatedBytes >= settings.targetBytes -> false
            playoutMs < settings.window.minMs -> true
            playoutMs >= settings.window.maxMs -> false
            else -> loading
        }
        return loading
    }

    fun shouldStart(settings: Settings, bufferedUs: Long, allocatedBytes: Int, speed: Float, rebuffering: Boolean): Boolean {
        val playoutMs = bufferedUs.coerceAtLeast(0) / (1_000.0 * safeSpeed(speed))
        val required = if (rebuffering) settings.window.resumeMs else settings.window.startMs
        // The byte limit wins: a high bitrate file must not wait for an unattainable time target.
        return bufferedUs > 0 && (playoutMs >= required || allocatedBytes >= settings.targetBytes)
    }

    @Synchronized fun resetLoading() { loading = false }
    private fun safeSpeed(speed: Float) = if (speed.isFinite() && speed > 0) speed.toDouble() else 1.0
}
