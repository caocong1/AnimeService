package com.fanyu.tv.player

/**
 * A one-shot user Play/Resume intent bound to one media: armed BEFORE the player is told to play, never while
 * that media is already playing, and consumed by the first real playback of the same media whether or not a
 * claim is needed. Playback of another media discards it; a recreated activity never re-consumes it.
 */
class PlayIntent {
    @Volatile var target: String? = null
        private set

    /** Registers the intent for [media] unless it is already playing. */
    fun arm(media: String?, alreadyPlaying: Boolean) {
        if (media != null && !alreadyPlaying) target = media
    }

    /** Re-targets a placeholder once the real media id is known. */
    fun retarget(from: String, to: String) { if (target == from) target = to }

    /** Consumes the intent on a real playback of [media]; true when a claim may be attempted for it. */
    fun consume(media: String?): Boolean {
        val hit = media != null && target == media
        target = null
        return hit
    }

    fun clear() { target = null }
}
