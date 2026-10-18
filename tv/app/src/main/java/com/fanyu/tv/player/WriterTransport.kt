package com.fanyu.tv.player

import com.fanyu.tv.data.Api
import com.fanyu.tv.data.ServerBinding

/**
 * Routes writer traffic through the app's API client (same Origin, token refresh, cookies and TLS) for ONE
 * server and connection generation: if the box is pointed elsewhere, pairing ends or a switch is rolled
 * back, the transport reports SERVER_CHANGED, a protocol failure that halts the writer instead of
 * replaying its packets against another server.
 */
class WriterTransport(private val api: Api, private val snapshot: ServerBinding.Snapshot = api.snapshot()) : PlaybackWriterClient.Transport {
    private fun reply(r: Api.StructuredReply) =
        PlaybackWriterClient.Response(if (r.status == Api.SERVER_CHANGED) PlaybackWriterClient.SERVER_CHANGED else r.status, r.body)

    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response =
        if (!api.accepts(snapshot)) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, body, snapshot))

    override suspend fun get(path: String): PlaybackWriterClient.Response =
        if (!api.accepts(snapshot)) PlaybackWriterClient.Response(PlaybackWriterClient.SERVER_CHANGED, null) else reply(api.structured(path, null, snapshot))
}
