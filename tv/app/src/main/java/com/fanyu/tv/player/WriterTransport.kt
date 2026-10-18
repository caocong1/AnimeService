package com.fanyu.tv.player

import com.fanyu.tv.data.Api

/**
 * Routes writer traffic through the app's API client (same Origin, token refresh, cookies and TLS) for ONE
 * server: if the box is pointed at another server the transport reports SERVER_CHANGED, a protocol
 * failure that halts the writer instead of replaying its packets elsewhere.
 */
class WriterTransport(private val api: Api, private val server: String = api.base) : PlaybackWriterClient.Transport {
    private fun reply(r: Api.StructuredReply) =
        PlaybackWriterClient.Response(if (r.status == Api.SERVER_CHANGED) PlaybackWriterClient.SERVER_CHANGED else r.status, r.body)

    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response =
        if (api.base != server) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, body, server))

    override suspend fun get(path: String): PlaybackWriterClient.Response =
        if (api.base != server) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, null, server))
}
