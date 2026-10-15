package com.fanyu.tv.player

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackBufferPolicyTest {
    private val mib = 1024L * 1024L

    @Test fun smallBoxKeepsMostOfItsHeapAvailable() {
        assertEquals(8 * mib, PlaybackBufferPolicy.targetBytes(64 * mib).toLong())
        assertEquals(16 * mib, PlaybackBufferPolicy.targetBytes(128 * mib).toLong())
    }

    @Test fun largeHeapDoesNotRestoreTheUnboundedVideoBuffer() {
        assertEquals(32 * mib, PlaybackBufferPolicy.targetBytes(256 * mib).toLong())
        assertEquals(32 * mib, PlaybackBufferPolicy.targetBytes(2 * 1024 * mib).toLong())
        assertEquals(32 * mib, PlaybackBufferPolicy.targetBytes(Long.MAX_VALUE).toLong())
    }

    @Test fun bufferCanStartAndResumeWithinItsTimeWindow() {
        assertTrue(PlaybackBufferPolicy.START_BUFFER_MS <= PlaybackBufferPolicy.MIN_BUFFER_MS)
        assertTrue(PlaybackBufferPolicy.REBUFFER_MS <= PlaybackBufferPolicy.MIN_BUFFER_MS)
        assertTrue(PlaybackBufferPolicy.MIN_BUFFER_MS <= PlaybackBufferPolicy.MAX_BUFFER_MS)
        assertEquals(2 * mib, PlaybackBufferPolicy.targetBytes(16 * mib).toLong())
    }
}
