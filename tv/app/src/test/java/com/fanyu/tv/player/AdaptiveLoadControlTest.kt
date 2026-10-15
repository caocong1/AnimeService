package com.fanyu.tv.player

import androidx.media3.common.C
import androidx.media3.common.Timeline
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.analytics.PlayerId
import androidx.media3.exoplayer.source.MediaSource.MediaPeriodId
import androidx.media3.exoplayer.upstream.Allocation
import androidx.media3.exoplayer.upstream.Allocator
import org.junit.Assert.*
import org.junit.Test

class AdaptiveLoadControlTest {
    private class FakeAllocator : Allocator {
        var bytes = 0
        override fun getTotalBytesAllocated() = bytes
        override fun getIndividualAllocationLength() = 65_536
        override fun allocate(): Allocation = error("No media allocation in policy tests")
        override fun release(allocation: Allocation) = Unit
        override fun release(node: Allocator.AllocationNode) = Unit
        override fun trim() = Unit
    }
    private class Delegate : LoadControl {
        val allocator = FakeAllocator()
        val events = mutableListOf<String>()
        override fun getAllocator(playerId: PlayerId): Allocator = allocator
        override fun onPrepared(playerId: PlayerId) { events += "prepared" }
        override fun onStopped(playerId: PlayerId) { events += "stopped" }
        override fun onReleased(playerId: PlayerId) { events += "released" }
        override fun getBackBufferDurationUs(playerId: PlayerId) = 0L
        override fun retainBackBufferFromKeyframe(playerId: PlayerId) = false
        override fun onTracksSelected(parameters: LoadControl.Parameters,
            trackGroups: androidx.media3.exoplayer.source.TrackGroupArray,
            selections: Array<out androidx.media3.exoplayer.trackselection.ExoTrackSelection?>) {
            events += "tracks"
        }
    }
    private fun params(bufferUs: Long, rebuffer: Boolean = false, at: Long = C.TIME_UNSET) =
        LoadControl.Parameters(PlayerId.UNSET, Timeline.EMPTY, MediaPeriodId("test"), 0, bufferUs,
            1f, true, rebuffer, C.TIME_UNSET, at)

    @Test fun realPlayerParametersEnforceCapAndPermitStartWhenTimeTargetCannotFit() {
        val policy = AdaptiveBufferPolicy(64L * 1024 * 1024)
        val delegate = Delegate()
        val load = AdaptiveLoadControl(policy, PlaybackTransferEstimator { _, _, _ -> }, delegate, { 1_000 }, { 0 })
        load.onPrepared(PlayerId.UNSET)
        assertSame(delegate.allocator, load.getAllocator(PlayerId.UNSET))
        assertTrue(load.shouldContinueLoading(params(0)))
        delegate.allocator.bytes = policy.hardTargetBytes
        assertFalse(load.shouldContinueLoading(params(1_000_000)))
        assertTrue(load.shouldStartPlayback(params(1_000_000, true, 1_000)))
        load.onStopped(PlayerId.UNSET)
        load.onReleased(PlayerId.UNSET)
        assertEquals(listOf("prepared", "stopped", "released"), delegate.events)
    }

    @Test fun playerConstructionAndTrackSelectionMustForwardJavaDefaultMethods() {
        val delegate = Delegate()
        val load = AdaptiveLoadControl(AdaptiveBufferPolicy(64L * 1024 * 1024),
            PlaybackTransferEstimator { _, _, _ -> }, delegate, { 0 }, { 0 })
        // ExoPlayer reads these during construction, before any file is opened.
        assertEquals(0L, load.getBackBufferDurationUs(PlayerId.UNSET))
        assertFalse(load.retainBackBufferFromKeyframe(PlayerId.UNSET))
        load.onPrepared(PlayerId.UNSET)
        load.onTracksSelected(params(0), androidx.media3.exoplayer.source.TrackGroupArray.EMPTY, emptyArray())
        assertEquals(listOf("prepared", "tracks"), delegate.events)
    }

    @Test fun everyCurrentMedia3HookHasAnExplicitImplementation() {
        // Prevent a Java default lifecycle hook from silently bypassing the adapter again.
        val hooks = LoadControl::class.java.declaredMethods.filter {
            !java.lang.reflect.Modifier.isStatic(it.modifiers) && !it.isAnnotationPresent(java.lang.Deprecated::class.java)
        }
        for (hook in hooks) {
            AdaptiveLoadControl::class.java.getDeclaredMethod(hook.name, *hook.parameterTypes)
        }
    }

    @Test fun seeksDoNotCountAsStallsAndRepeatedChecksDoNotExtendARealStall() {
        val policy = AdaptiveBufferPolicy(128L * 1024 * 1024)
        val delegate = Delegate()
        var clock = 1_000L
        val load = AdaptiveLoadControl(policy, PlaybackTransferEstimator { _, _, _ -> }, delegate, { clock }, { 0 })
        assertTrue(load.shouldStartPlayback(params(2_500_000))) // Initial start / seek.
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, policy.settings(clock, 0).mode)
        assertFalse(load.shouldStartPlayback(params(5_000_000, true, 1_000)))
        clock = 121_000
        load.shouldContinueLoading(params(0, true, 1_000))
        clock = 151_000
        load.shouldContinueLoading(params(0, true, 1_000))
        assertEquals(AdaptiveBufferPolicy.Mode.UNKNOWN, policy.settings(clock, 0).mode)
    }
}
