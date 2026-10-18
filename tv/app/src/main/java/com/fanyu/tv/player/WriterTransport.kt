package com.fanyu.tv.player

import com.fanyu.tv.data.Api
import com.fanyu.tv.data.AuthState

/**
 * Routes writer traffic through the app's API client (same Origin, token refresh, cookies and TLS) for ONE
 * server generation and ONE negotiated capability: every attempt samples one immutable context of the bound
 * generation and sends with exactly that object; a moved generation is SERVER_CHANGED (a protocol failure),
 * a context that no longer advertises the capability is CAPABILITY_WITHDRAWN (disabled), both checked on the
 * context that would send, first send and retry alike. A token refresh inside a call stays bound to the same
 * server and generation, and a refresh that fails is delivered with its HTTP status so the writer applies its
 * own authorization/protocol/disabled boundaries instead of treating it as a network fault.
 */
class WriterTransport(private val api: Api, private val bound: AuthState.Context = api.context()) : PlaybackWriterClient.Transport {
    private fun reply(r: Api.StructuredReply) = PlaybackWriterClient.Response(when (r.status) {
        Api.SERVER_CHANGED -> PlaybackWriterClient.SERVER_CHANGED
        Api.CAPABILITY_WITHDRAWN -> PlaybackWriterClient.CAPABILITY_WITHDRAWN
        else -> r.status
    }, r.body)

    private fun negotiated(ctx: AuthState.Context) = PlaybackWriterClient.negotiated(ctx.capabilities)

    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response =
        reply(api.structured(path, body, bound, ::negotiated))

    override suspend fun get(path: String): PlaybackWriterClient.Response =
        reply(api.structured(path, null, bound, ::negotiated))
}
