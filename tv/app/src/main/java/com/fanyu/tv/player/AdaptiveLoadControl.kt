package com.fanyu.tv.player

import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.common.Timeline
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.analytics.PlayerId
import androidx.media3.exoplayer.source.MediaSource.MediaPeriodId
import androidx.media3.exoplayer.source.TrackGroupArray
import androidx.media3.exoplayer.trackselection.ExoTrackSelection
import androidx.media3.exoplayer.upstream.Allocator

/** Keep Media3 allocator/lifecycle handling, with bounded adaptive loading and start decisions. */
@OptIn(UnstableApi::class)
internal class AdaptiveLoadControl(
    private val policy: AdaptiveBufferPolicy,
    private val transfers: PlaybackTransferEstimator,
    private val delegate: LoadControl = DefaultLoadControl.Builder()
        .setTargetBufferBytes(policy.hardTargetBytes)
        .setPrioritizeTimeOverSizeThresholds(false)
        .build(),
    private val now: () -> Long = { SystemClock.elapsedRealtime() },
    private val usedHeap: () -> Long = { Runtime.getRuntime().let { it.totalMemory() - it.freeMemory() } },
) : LoadControl {
    // Kotlin `by` forwards abstract Java methods, but not these Java default methods.
    // Media3 defaults intentionally throw; every current lifecycle/configuration hook must be explicit.
    override fun getAllocator(playerId: PlayerId): Allocator = delegate.getAllocator(playerId)

    override fun getBackBufferDurationUs(playerId: PlayerId): Long = delegate.getBackBufferDurationUs(playerId)

    override fun retainBackBufferFromKeyframe(playerId: PlayerId): Boolean = delegate.retainBackBufferFromKeyframe(playerId)

    override fun onTracksSelected(parameters: LoadControl.Parameters, trackGroups: TrackGroupArray,
        trackSelections: Array<out ExoTrackSelection?>) = delegate.onTracksSelected(parameters, trackGroups, trackSelections)

    override fun shouldContinuePreloading(playerId: PlayerId, timeline: Timeline,
        mediaPeriodId: MediaPeriodId, bufferedDurationUs: Long): Boolean = false

    override fun onPrepared(playerId: PlayerId) {
        policy.resetLoading()
        delegate.onPrepared(playerId)
    }

    override fun shouldContinueLoading(parameters: LoadControl.Parameters): Boolean {
        val at = now()
        policy.recordRebuffer(parameters.lastRebufferRealtimeMs)
        val settings = policy.settings(at, usedHeap(), parameters.playbackSpeed)
        val load = policy.shouldLoad(settings, parameters.bufferedDurationUs,
            delegate.getAllocator(parameters.playerId).totalBytesAllocated, parameters.playbackSpeed)
        transfers.loadingAllowed(load, at)
        return load
    }

    override fun shouldStartPlayback(parameters: LoadControl.Parameters): Boolean {
        policy.recordRebuffer(parameters.lastRebufferRealtimeMs)
        return policy.shouldStart(policy.settings(now(), usedHeap(), parameters.playbackSpeed),
            parameters.bufferedDurationUs, delegate.getAllocator(parameters.playerId).totalBytesAllocated,
            parameters.playbackSpeed, parameters.rebuffering)
    }

    override fun onStopped(playerId: PlayerId) {
        transfers.loadingAllowed(false, now())
        policy.resetLoading()
        delegate.onStopped(playerId)
    }

    override fun onReleased(playerId: PlayerId) {
        transfers.loadingAllowed(false, now())
        policy.resetLoading()
        delegate.onReleased(playerId)
    }
}
