package com.fanyu.tv.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** 场次 dark from static/tokens.css, converted from OKLCH (see .design/tv/brief.md). */
object C {
    val bg = Color(0xFF1E1C1B)
    val layer1 = Color(0xFF262422)
    val layer2 = Color(0xFF312E2C)
    val line = Color(0xFF423F3D)
    val lineStrong = Color(0xFF7D7977)
    val ink = Color(0xFFF1F0ED)
    val muted = Color(0xFFBCBAB6)
    val dim = Color(0xFFA6A4A0)
    val signal = Color(0xFF6EDCB9)
    val onSignal = Color(0xFF0B1F18)
    val signalWash = Color(0xFF1F3C32)
    val warn = Color(0xFFF0BE67)
    val danger = Color(0xFFFB8274)
}

object T {
    val title = TextStyle(color = C.ink, fontSize = 26.sp, lineHeight = 34.sp, fontWeight = FontWeight.SemiBold)
    val heading = TextStyle(color = C.ink, fontSize = 22.sp, lineHeight = 30.sp, fontWeight = FontWeight.SemiBold)
    val section = TextStyle(color = C.ink, fontSize = 15.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold)
    val body = TextStyle(color = C.ink, fontSize = 15.sp, lineHeight = 22.sp)
    val meta = TextStyle(color = C.muted, fontSize = 14.sp, lineHeight = 20.sp)
    val small = TextStyle(color = C.dim, fontSize = 13.sp, lineHeight = 18.sp)
    val label = TextStyle(color = C.dim, fontSize = 12.sp, lineHeight = 16.sp)
}

val SafeH = 48.dp
val SafeV = 27.dp
val Ellipsis = TextOverflow.Ellipsis
