package com.fanyu.tv.player

import com.fanyu.tv.data.Api

/** Routes writer traffic through the app's API client: same Origin, token refresh, cookies and TLS checks; structured replies. */
class WriterTransport(private val api: Api) : PlaybackWriterClient.Transport {
    override suspend fun post(path: String, body: Map<String, Any?>, keepalive: Boolean): PlaybackWriterClient.Response {
        val r = api.structured(path, body)
        return PlaybackWriterClient.Response(r.status, r.body)
    }

    override suspend fun get(path: String): PlaybackWriterClient.Response {
        val r = api.structured(path, null)
        return PlaybackWriterClient.Response(r.status, r.body)
    }
}
