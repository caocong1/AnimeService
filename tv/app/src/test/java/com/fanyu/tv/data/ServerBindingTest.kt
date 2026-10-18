package com.fanyu.tv.data

import org.junit.Assert.*
import org.junit.Test

class ServerBindingTest {
    @Test fun aSwitchAwayAndBackIsANewGeneration() {
        val b = ServerBinding()
        val a = b.bind("http://a:4871")
        assertTrue(b.accepts(a, "http://a:4871"))
        assertFalse(b.accepts(a, "http://b:4871"))         // A → B
        assertFalse(b.accepts(a, "http://a:4871"))         // A → B → A: still a different generation
        val again = b.bind("http://a:4871")
        assertTrue(again.generation > a.generation)
    }

    @Test fun invalidationOnTheSameServerRefusesOldSnapshots() {
        val b = ServerBinding()
        val s = b.bind("http://a:4871")
        b.invalidate()                                      // pairing ended / failed switch rolled back
        assertFalse(b.accepts(s, "http://a:4871"))
        assertTrue(b.accepts(b.bind("http://a:4871"), "http://a:4871"))
    }
}
