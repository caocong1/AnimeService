import java.security.MessageDigest
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// One version per closed loop; release.json carries it to installed boxes.
val appVersionCode = 1
val appVersionName = "1.0.0"

val signing = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.isFile) file.inputStream().use { load(it) }
}

android {
    namespace = "com.fanyu.tv"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.fanyu.tv"
        minSdk = 23
        targetSdk = 36
        versionCode = appVersionCode
        versionName = appVersionName
    }

    signingConfigs {
        if (signing.isNotEmpty()) create("release") {
            storeFile = rootProject.file(signing.getProperty("storeFile"))
            storePassword = signing.getProperty("storePassword")
            keyAlias = signing.getProperty("keyAlias")
            keyPassword = signing.getProperty("keyPassword")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

dependencies {
    val compose = platform("androidx.compose:compose-bom:2026.06.01")
    implementation(compose)
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.media3:media3-exoplayer:1.11.1")
    implementation("androidx.media3:media3-ui:1.11.1")
    implementation("androidx.media3:media3-datasource-okhttp:1.11.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("io.coil-kt:coil-compose:2.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
}

// Writes build/publish/{fanyu-tv.apk,release.json}; copy both into the server's data/tv/.
tasks.register("publishRelease") {
    dependsOn("assembleRelease")
    doLast {
        val apk = layout.buildDirectory.file("outputs/apk/release/app-release.apk").get().asFile
        val out = layout.buildDirectory.dir("publish").get().asFile.apply { mkdirs() }
        val target = File(out, "fanyu-tv.apk")
        apk.copyTo(target, overwrite = true)
        val digest = MessageDigest.getInstance("SHA-256").digest(target.readBytes()).joinToString("") { "%02x".format(it) }
        val notes = rootProject.file("release-notes.txt").takeIf { it.isFile }?.readLines()
            ?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()
        val json = buildString {
            append("{\n  \"version_code\": $appVersionCode,\n  \"version_name\": \"$appVersionName\",\n")
            append("  \"size\": ${target.length()},\n  \"sha256\": \"$digest\",\n  \"notes\": [")
            append(notes.joinToString(", ") { "\"" + it.replace("\\", "\\\\").replace("\"", "\\\"") + "\"" })
            append("]\n}\n")
        }
        File(out, "release.json").writeText(json)
        println("Published ${target.absolutePath} ($appVersionName, $appVersionCode)")
    }
}
