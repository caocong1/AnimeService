package com.fanyu.tv.player

/** Samples while network reads are allowed; deliberate buffer-full pauses do not dilute the rate. */
internal class PlaybackTransferEstimator(private val sample: (Long, Long, Long) -> Unit) {
    private var allowed = false
    private var transfers = 0
    private var sinceMs = -1L
    private var bytes = 0L

    @Synchronized fun loadingAllowed(value: Boolean, nowMs: Long) {
        if (allowed == value) return
        if (!value) emit(nowMs)
        allowed = value
        restart(nowMs)
    }
    @Synchronized fun transferStarted(nowMs: Long) {
        if (transfers++ == 0) restart(nowMs)
    }
    @Synchronized fun bytesTransferred(count: Int, nowMs: Long) {
        if (!allowed || transfers == 0 || count <= 0) return
        bytes += count
        if (nowMs - sinceMs >= 1_000) { emit(nowMs); restart(nowMs) }
    }
    @Synchronized fun transferEnded(nowMs: Long) {
        if (transfers == 0) return
        if (--transfers == 0) { emit(nowMs); restart(nowMs) }
    }
    private fun emit(nowMs: Long) {
        if (allowed && sinceMs >= 0 && bytes > 0) sample(bytes, (nowMs - sinceMs).coerceAtLeast(0), nowMs)
    }
    private fun restart(nowMs: Long) { sinceMs = nowMs; bytes = 0 }
}
