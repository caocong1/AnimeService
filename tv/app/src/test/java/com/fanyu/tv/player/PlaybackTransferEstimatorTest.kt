package com.fanyu.tv.player

import org.junit.Assert.*
import org.junit.Test

class PlaybackTransferEstimatorTest {
    @Test fun bufferFullPauseIsExcludedFromSubsequentNetworkMeasurement() {
        val samples = mutableListOf<Triple<Long, Long, Long>>()
        val meter = PlaybackTransferEstimator { bytes, elapsed, at -> samples.add(Triple(bytes, elapsed, at)) }
        meter.loadingAllowed(true, 0)
        meter.transferStarted(100)
        meter.bytesTransferred(1_000_000, 1_100)
        meter.loadingAllowed(false, 1_100)
        meter.bytesTransferred(123, 5_000) // In-flight read after loading was suspended.
        meter.loadingAllowed(true, 21_100)
        meter.bytesTransferred(1_000_000, 22_100)
        assertEquals(listOf(Triple(1_000_000L, 1_000L, 1_100L), Triple(1_000_000L, 1_000L, 22_100L)), samples)
    }

    @Test fun overlappingTransfersUseAggregateBytesAndDoNotResetEachOther() {
        val samples = mutableListOf<Pair<Long, Long>>()
        val meter = PlaybackTransferEstimator { bytes, elapsed, _ -> samples.add(bytes to elapsed) }
        meter.loadingAllowed(true, 0)
        meter.transferStarted(0)
        meter.bytesTransferred(200_000, 200)
        meter.transferStarted(300)
        meter.bytesTransferred(300_000, 500)
        meter.transferEnded(600)
        meter.bytesTransferred(500_000, 1_000)
        meter.transferEnded(1_000)
        meter.transferEnded(1_100)
        assertEquals(listOf(1_000_000L to 1_000L), samples)
    }

    @Test fun shortFinalWindowIsEmittedOnSuspendAndTransferEndWithoutDuplicates() {
        val samples = mutableListOf<Pair<Long, Long>>()
        val meter = PlaybackTransferEstimator { bytes, elapsed, _ -> samples.add(bytes to elapsed) }
        meter.loadingAllowed(true, 0); meter.transferStarted(0)
        meter.bytesTransferred(500_000, 500)
        meter.loadingAllowed(false, 600)
        meter.transferEnded(800)
        meter.loadingAllowed(true, 1_000); meter.transferStarted(1_000)
        meter.bytesTransferred(500_000, 1_500); meter.transferEnded(1_600)
        assertEquals(listOf(500_000L to 600L, 500_000L to 600L), samples)
    }
}
