package com.fanyu.tv.player

import org.junit.Assert.*
import org.junit.Test

class PlayIntentTest {
    @Test fun armedBeforePlayAndConsumedOnceByTheSameMedia() {
        val i = PlayIntent()
        i.arm("m1", alreadyPlaying = false)
        assertTrue(i.consume("m1"))
        assertFalse(i.consume("m1"))                      // a buffering resume later finds nothing to consume
    }

    @Test fun anotherMediaDiscardsTheIntentAndAlreadyPlayingNeverArms() {
        val i = PlayIntent()
        i.arm("m1", alreadyPlaying = false)
        assertFalse(i.consume("m2")); assertNull(i.target)
        i.arm("m1", alreadyPlaying = true)
        assertNull(i.target); assertFalse(i.consume("m1"))
    }

    @Test fun placeholderIsRetargetedOnceTheMediaIsKnownAndRecreationDoesNotReplay() {
        val i = PlayIntent()
        i.arm("*first*", alreadyPlaying = false)
        assertFalse(i.consume("m1"))                      // the real id is not known yet: nothing consumed, placeholder dropped
        val j = PlayIntent()
        j.arm("*first*", alreadyPlaying = false)
        j.retarget("*first*", "m1")
        assertTrue(j.consume("m1")); assertFalse(j.consume("m1"))
        j.clear(); assertNull(j.target)
    }
}
