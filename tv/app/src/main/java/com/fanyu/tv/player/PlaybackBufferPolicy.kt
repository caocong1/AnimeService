package com.fanyu.tv.player

/** Leave room in the app heap for the MP4 index, comments, artwork and the UI. */
internal object PlaybackBufferPolicy {
    const val MIN_BUFFER_MS = 10_000
    const val MAX_BUFFER_MS = 30_000
    const val START_BUFFER_MS = 2_500
    const val REBUFFER_MS = 5_000

    private const val MIB = 1024L * 1024L

    fun targetBytes(maxHeapBytes: Long): Int {
        require(maxHeapBytes > 0)
        return (maxHeapBytes / 8).coerceIn(2 * MIB, 32 * MIB).toInt()
    }
}
