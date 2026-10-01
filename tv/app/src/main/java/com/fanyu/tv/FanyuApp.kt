package com.fanyu.tv

import android.app.Application
import android.content.Context
import coil.ImageLoader
import coil.ImageLoaderFactory
import com.fanyu.tv.data.Api
import com.fanyu.tv.data.Prefs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class FanyuApp : Application(), ImageLoaderFactory {
    lateinit var prefs: Prefs
        private set
    lateinit var api: Api
        private set

    /** Outlives an activity, so the last progress report still goes out after the player closes. */
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        api = Api(prefs)
    }

    override fun newImageLoader() = ImageLoader.Builder(this)
        .okHttpClient { api.client }
        .crossfade(true)
        .build()
}

val Context.app get() = applicationContext as FanyuApp
