package com.fanyu.tv.data

/**
 * A monotonic connection generation: every server change, pairing exit or explicit invalidation bumps it.
 * Requests take a snapshot before they start and may only be accepted while the same server and
 * generation are still current, so A→B→A, a failed switch rolled back, or a late answer never hands an
 * old server's token, capability or reply to the current one.
 */
class ServerBinding {
    data class Snapshot(val server: String, val generation: Long)

    @Volatile var generation: Long = 0
        private set
    @Volatile var server: String = ""
        private set

    /** Tracks the configured server; a different value is a new generation. */
    @Synchronized fun bind(current: String): Snapshot {
        if (current != server) { server = current; generation++ }
        return Snapshot(server, generation)
    }

    /** Pairing ended or the server was forgotten: nothing issued for the old generation may be reused. */
    @Synchronized fun invalidate() { generation++ }

    @Synchronized fun accepts(snapshot: Snapshot, current: String): Boolean {
        bind(current)
        return snapshot.server == server && snapshot.generation == generation
    }
}
