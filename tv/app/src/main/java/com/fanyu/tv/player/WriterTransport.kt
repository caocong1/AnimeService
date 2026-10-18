package com.fanyu.tv.player

import com.fanyu.tv.data.Api
import com.fanyu.tv.data.AuthState

/**
 * Routes writer traffic through the app's API client (same Origin, token refresh, cookies and TLS) for ONE
 * server generation: every call re-captures the authentication context and refuses to send when the
 * generation moved (server switched, rolled back or pairing ended), reporting SERVER_CHANGED, a protocol
 * failure that halts the writer instead of replaying its packets against another server.
 */
class WriterTransport(private val api: Api, private val bound: AuthState.Context = api.context()) : PlaybackWriterClient.Transport {
    private fun reply(r: Api.StructuredReply) =
        PlaybackWriterClient.Response(if (r.status == Api.SERVER_CHANGED) PlaybackWriterClient.SERVER_CHANGED else r.status, r.body)

    private fun changed() = api.context().snapshot != bound.snapshot

    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response =
        if (changed()) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, body, bound))

    override suspend fun get(path: String): PlaybackWriterClient.Response =
        if (changed()) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, null, bound))
}
