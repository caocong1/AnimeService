package com.fanyu.tv.player

import com.fanyu.tv.data.Api
import com.fanyu.tv.data.AuthState

/**
 * Routes writer traffic through the app's API client (same Origin, token refresh, cookies and TLS) for ONE
 * server generation and ONE negotiated capability: every call re-captures the authentication context and
 * refuses to send when the generation moved (SERVER_CHANGED, a protocol failure) or the capability is no
 * longer advertised (CAPABILITY_WITHDRAWN, disabled); a token refresh inside a call is subject to the same
 * two checks before anything is replayed, and a refresh that fails is delivered with its HTTP status so the
 * writer applies its own authorization/protocol/disabled boundaries instead of treating it as a network fault.
 */
class WriterTransport(private val api: Api, private val bound: AuthState.Context = api.context()) : PlaybackWriterClient.Transport {
    private fun reply(r: Api.StructuredReply) = PlaybackWriterClient.Response(when (r.status) {
        Api.SERVER_CHANGED -> PlaybackWriterClient.SERVER_CHANGED
        Api.CAPABILITY_WITHDRAWN -> PlaybackWriterClient.CAPABILITY_WITHDRAWN
        else -> r.status
    }, r.body)

    private fun negotiated(ctx: AuthState.Context) = PlaybackWriterClient.negotiated(ctx.capabilities)

    private fun blocked(): PlaybackWriterClient.Response? {
        val ctx = api.context()
        return when {
            ctx.snapshot != bound.snapshot -> PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null)
            !negotiated(ctx) -> PlaybackWriterClient.Response(PlaybackWriterClient.CAPABILITY_WITHDRAWN, null)
            else -> null
        }
    }

    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response =
        blocked() ?: reply(api.structured(path, body, bound, ::negotiated))

    override suspend fun get(path: String): PlaybackWriterClient.Response =
        blocked() ?: reply(api.structured(path, null, bound, ::negotiated))
}
