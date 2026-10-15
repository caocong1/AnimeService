package com.fanyu.tv.player

import org.junit.Assert.*
import org.junit.Test

class AdaptiveBufferPolicyTest {
    private val mib = 1024L * 1024
    private fun policy() = AdaptiveBufferPolicy(128 * mib).apply {
        setMediaSize(100_000_000) // Exactly 8 Mbit/s including audio/container.
        setMediaDuration(100_000)
    }
    private fun sample(p: AdaptiveBufferPolicy, at: Long, mbps: Int = 32) = p.recordTransfer(mbps * 125_000L, 1_000, at)
    private fun stableFast(p: AdaptiveBufferPolicy) {
        for (at in 1_000L..3_000L step 1_000) sample(p, at)
        p.settings(3_000, 0)
        sample(p, 33_000)
    }

    @Test fun unknownNetworkPreservesWorkingReleaseAndSmallHeapCap() {
        val p = policy()
        val s = p.settings(0, 0)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, s.mode)
        assertEquals(AdaptiveBufferPolicy.Window(10_000, 30_000, 2_500, 5_000), s.window)
        assertEquals(16 * mib, s.targetBytes.toLong())
        assertFalse(p.shouldLoad(s, 0, s.targetBytes, 1f))
    }

    @Test fun fastNetworkRequiresMeasuredSamplesAndStablePeriod() {
        val p = policy()
        sample(p, 1_000); sample(p, 2_000)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(2_000, 0).mode)
        sample(p, 3_000)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(3_000, 0).mode)
        sample(p, 32_999)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(32_999, 0).mode)
        assertEquals(AdaptiveBufferPolicy.Mode.FAST, p.settings(33_000, 0).mode)
    }

    @Test fun slowNetworkAndVariabilityIncreaseProtectionImmediately() {
        val slow = policy()
        for (at in 1_000L..3_000L step 1_000) sample(slow, at, 6)
        assertEquals(AdaptiveBufferPolicy.Mode.CAUTIOUS, slow.settings(3_000, 0).mode)
        val variable = policy()
        sample(variable, 1_000, 40); sample(variable, 2_000, 1); sample(variable, 3_000, 100)
        assertEquals(AdaptiveBufferPolicy.Mode.CAUTIOUS, variable.settings(3_000, 0).mode)
    }

    @Test fun bandwidthOnlyMattersWhenActualMediaBitrateIsKnown() {
        val p = AdaptiveBufferPolicy(128 * mib)
        for (at in 1_000L..3_000L step 1_000) sample(p, at)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(40_000, 0).mode)
        p.setMediaSize(100_000_000); p.setMediaDuration(100_000)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(40_000, 0).mode)
        sample(p, 55_000); sample(p, 70_000)
        assertEquals(AdaptiveBufferPolicy.Mode.FAST, p.settings(70_000, 0).mode)
    }

    @Test fun staleMeasurementsAndTinyIndexReadsCannotSelectFastMode() {
        val p = policy()
        p.recordTransfer(1024, 1_000, 1_000)
        p.recordTransfer(1_000_000, 1, 2_000)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(35_000, 0).mode)
        stableFast(p)
        assertEquals(AdaptiveBufferPolicy.Mode.FAST, p.settings(33_000, 0).mode)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(94_000, 0).mode)
        sample(p, 95_000)
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, p.settings(95_000, 0).mode)
    }

    @Test fun realRebufferOverridesFastNetworkAndRelaxationIsDelayed() {
        val p = policy()
        stableFast(p)
        assertEquals(AdaptiveBufferPolicy.Mode.FAST, p.settings(33_000, 0).mode)
        p.recordRebuffer(34_000)
        assertEquals(AdaptiveBufferPolicy.Mode.CAUTIOUS, p.settings(34_000, 0).mode)
        sample(p, 60_000); sample(p, 90_000); sample(p, 120_000)
        sample(p, 153_000)
        assertEquals(AdaptiveBufferPolicy.Mode.CAUTIOUS, p.settings(153_000, 0).mode)
        assertEquals(AdaptiveBufferPolicy.Mode.CAUTIOUS, p.settings(154_000, 0).mode)
        sample(p, 184_000)
        assertEquals(AdaptiveBufferPolicy.Mode.FAST, p.settings(184_000, 0).mode)
    }

    @Test fun heapPressureLowersBudgetWithoutExceedingOriginalCapAndRecoversSlowly() {
        val p = policy()
        assertEquals(8 * mib, p.settings(0, 100 * mib).targetBytes.toLong())
        assertEquals(8 * mib, p.settings(1_000, 70 * mib).targetBytes.toLong())
        assertEquals(8 * mib, p.settings(30_999, 70 * mib).targetBytes.toLong())
        assertEquals(16 * mib, p.settings(31_000, 70 * mib).targetBytes.toLong())
        val small = AdaptiveBufferPolicy(16 * mib)
        assertEquals(2 * mib, small.settings(0, 15 * mib).targetBytes.toLong())
    }

    @Test fun byteLimitAllowsHighBitratePlaybackWithoutDeadlock() {
        val p = policy()
        p.recordRebuffer(0)
        val s = p.settings(0, 0)
        assertFalse(p.shouldLoad(s, 500_000, s.targetBytes, 1f))
        assertTrue(p.shouldStart(s, 500_000, s.targetBytes, 1f, true))
        assertFalse(p.shouldStart(s, 0, s.targetBytes, 1f, true))
        assertFalse(p.shouldStart(s, 500_000, s.targetBytes - 1, 1f, true))
    }

    @Test fun remainingBufferAndPlaybackSpeedControlLoadingWithHysteresis() {
        val p = policy()
        stableFast(p)
        val s = p.settings(33_000, 0)
        assertTrue(p.shouldLoad(s, 7_000_000, 0, 1f))
        assertTrue(p.shouldLoad(s, 10_000_000, 0, 1f))
        assertFalse(p.shouldLoad(s, 15_000_000, 0, 1f))
        assertFalse(p.shouldLoad(s, 10_000_000, 0, 1f))
        assertTrue(p.shouldLoad(s, 10_000_000, 0, 2f))
        assertFalse(p.shouldStart(s, 1_000_000, 0, 2f, false))
        assertTrue(p.shouldStart(s, 2_000_000, 0, 2f, false))
    }

    @Test fun mixedHardwareAndNetworkNeverIncreaseMemoryBudget() {
        for (heapMiB in listOf(16, 64, 128, 256, 2048)) {
            val p = AdaptiveBufferPolicy(heapMiB * mib)
            p.setMediaSize(100_000_000); p.setMediaDuration(100_000)
            for (tick in 1L..1_000L) {
                val at = tick * 1_000
                sample(p, at, if (tick % 5 == 0L) 4 else 40)
                if (tick % 40 == 0L) p.recordRebuffer(at)
                val s = p.settings(at, if (tick % 50 < 10) heapMiB * mib * 4 / 5 else 0)
                assertTrue(s.targetBytes <= p.hardTargetBytes)
                assertTrue(s.window.maxMs <= 30_000)
                assertTrue(s.window.startMs <= s.window.minMs)
                assertTrue(s.window.resumeMs <= s.window.minMs)
                assertFalse(p.shouldLoad(s, 0, s.targetBytes, 1f))
            }
        }
    }
}
