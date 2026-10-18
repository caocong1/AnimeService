package com.fanyu.tv.data

/**
 * The authentication context of one server generation, handled atomically: the server snapshot, the token
 * and the capabilities issued for it live together, are captured together before a request, are installed
 * only by a compare-and-swap on the same generation, and are dropped together when the server changes, a
 * switch is rolled back or pairing ends. The network is never called while the monitor is held.
 */
class AuthState {
    data class Context(val snapshot: ServerBinding.Snapshot, val token: String?, val capabilities: Map<String, Any?>)

    private val lock = Any()
    private val binding = ServerBinding()
    private var token: String? = null
    private var capabilities: Map<String, Any?> = emptyMap()
    private var installedFor: ServerBinding.Snapshot? = null

    /** Captures everything a request needs for the configured [server]; a server change drops the old credentials first. */
    fun context(server: String): Context = synchronized(lock) {
        val snap = binding.bind(server)
        if (installedFor != snap) { token = null; capabilities = emptyMap(); installedFor = null }
        Context(snap, token, capabilities)
    }

    /** Installs a bootstrap result only if the generation it was requested for is still the current one; the installed context, or null. */
    fun install(ctx: Context, server: String, newToken: String, newCapabilities: Map<String, Any?>): Context? = synchronized(lock) {
        if (binding.bind(server) != ctx.snapshot) return null
        token = newToken; capabilities = newCapabilities; installedFor = ctx.snapshot
        Context(ctx.snapshot, newToken, newCapabilities)
    }

    /** Still the same server and generation the context was captured for. */
    fun accepts(ctx: Context, server: String): Boolean = synchronized(lock) { binding.bind(server) == ctx.snapshot }

    /** The server was forgotten, a switch rolled back or pairing ended: a new generation with nothing issued. */
    fun invalidate() = synchronized(lock) { binding.invalidate(); token = null; capabilities = emptyMap(); installedFor = null }
}
