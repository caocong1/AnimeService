# 网页画质核查与实现（2026-10-10）

用户要求追求网页播放的极致画面。当前结论：服务没有视频降码率转码，但不能把原码流保持、能够播放和端到端画质完美还原混为一谈。浏览器解码器、色彩管理、系统 HDR、屏幕和显示尺寸都参与最终结果。未取得源母版，也未通过校色设备验证 HDR/10-bit 最终显示，不宣称所有编码、所有设备完美。

## 已落实

- 默认原生视频解码与显示；原文件直出，原有异常 MP4 及音轨缓存仅复制视频包。不改原媒体、片源/下载规则或观看记录。
- 原生画面按屏幕像素比计算整数显示尺寸，保持解码后宽高比和黑边，不裁切、强拉伸、滤镜或额外 CSS transform。原像素模式不放大，空间不足则按比例缩小。非方形像素按浏览器解码后的显示比例，不冒称编码采样点 1:1。
- 可选 Lanczos 3 SDR 放大：WebGL2，6×6 核、邻近像素限制过冲；按 requestVideoFrameCallback 调度。仅明确标注的 8-bit、BT.709 SDR、范围已知、逐行、方形像素，放大 1–2.5 倍，纹理/画布不超过 GPU 上限或 4096。无 AI 超分、补帧、去噪/锐化，不恢复编码损失的细节。
- HDR、10-bit、未知色彩、缩小、不支持逐帧 API、画中画、超尺寸保留原生。显卡错误/上下文丢失回退；2秒窗口至少20帧且掉帧超过8%时回退。掉帧由浏览器报告，不能据此断言 GPU 模式或实际屏幕位深。
- 画面选择在页面和全屏设置中可用；偏好仅存浏览器，切换不 seek、不改变音轨/音量/倍速/暂停、不标记已看。画质信息展示原片编码/profile、像素格式/位深、帧率、色域/传递函数/矩阵/范围、原片与实际解码尺寸、显示像素/比例、浏览器掉帧统计。未标注保留未知，不强猜 BT.709 或强制扩展有限范围。
- 封装/音轨不兼容时，提供“原码流兼容播放”：使用既有有界 AudioTracks 缓存，选择文件默认标记音轨（无标记才首音轨），视频 copy；AAC/MP3 音频 copy，其他音频沿原 AAC 兼容转换。只换容器不能使不支持的原视频编码变得可解码；无音轨/不在原支持范围的编码不提供此按钮。复用原 token/Origin 门禁，准备期间不写播放进度，失败不删除原文件。
- 修复错误/播放结束层被 video 挡住的层级问题，兼容按钮可以真正点击。字幕、弹幕、截图和播放器控件维持各自图层；截图仍是浏览器截图，不能当作 HDR 输出证明。

## 实测及限制

证据：`picture-fidelity-20261010.json`、`picture-browser-20261010.json`、`picture-live-20261010.json`、`picture-deployment-20261010.json`；同名复现脚本和 desktop/mobile/live PNG 保留。浏览器均为真实 Edge headless，与用户当前浏览器/物理 HDR 屏幕并不等同。修复后验证在文档 fetch 和 context 网络两层拦截生产写入，并替换自动弹幕 API，重跑生产观看/匹配写入为0。

首轮 live 验证页面切换时，一条 keepalive 进度漏过 page.route：media b866ea894cc1510cdb1221de5b2acf78 的position写成测试精确暂停时间1228.048453、updated1791563976.7572877；duration、last_played、sources、watches及其它保护字段相同。只在事务中核实整行仍与这条测试请求一致后，恢复该行position/updated为部署前原值，不覆盖其他行/用户操作。审计保留deployment JSON的fixture_progress_correction。双层拦截重跑后所有保护摘要重新一致；首轮并非0写入，不掩盖此事故。

- 14个可见片库样本只读 ffprobe：H.264 8-bit、HEVC 10-bit，包含明确 SDR 与色彩未知文件。历史增强文件仅用于读参数，不是新取样、主源推荐或片源政策变更。
- 生成 H.264 8-bit / H.264 10-bit / HEVC 10-bit / AV1 10-bit 四种 480×270 测试片，各72帧，MKV→MP4 copy 前后解码帧大小/哈希逐帧一致；原文件不动。Edge 四种均实际出画面。这个结果不覆盖全部 codec profile、level、色度采样、封装、音轨或 HDR，也不证明显示器输出10-bit。
- GPU SDR 缩放抽查2040个通道值，和 CPU 同核参考的最大误差约0.53/255，GL error=0。证明缩放实现/方向/采样正常，不是比源母版更清晰的证明或对全部视频的色彩校准。
- 测试验证物理像素尺寸、暂停/时间保持、上下文丢失、画中画、注入掉帧保护、HDR/未知参数回退、读取失败、封装错误恢复、移动端无页面溢出、全屏入口。实际动画 H264/HEVC10 的稳定播放计数以 live JSON 为准，启动/seek 掉帧不隐藏。
- 93项隔离 Python 测试、3项 Node 几何/门禁测试通过；既有 remote_ui 测试把后台旧 core/manage v2 写死，当前后台 v4/v3 已在旧 HANDOFF 记录，本轮排除该一条，未更改后台页面或该断言。保留1条既有 TestClient 弃用警告。音轨浏览器回归通过，包括位置/音量/倍速/暂停/字幕/全屏/记忆/失败/取消。
- 安全空闲重载前在线备份，保留 supervisor；其虚拟环境转发器启动较慢，最终原监督链自行恢复，无改启动配置/下载器。上线 API 200 / static v27 / quick_check ok；个人字段、watches、web_progress、设备会话和不确定提交摘要前后相同。

## 技术依据

- [FFmpeg streamcopy](https://ffmpeg.org/ffmpeg-all.html#Streamcopy)：不解码和编码，避免重编码损失；并非任何容器/轨道都能直接 copy。
- [ffprobe](https://ffmpeg.org/ffprobe.html)：只读检查原流属性。用实际标签，不推断原生制作分辨率或无隐藏处理。
- [requestVideoFrameCallback](https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback)：按视频合成帧调度，主线程回调不保证绝对同步。
- [WebGL texImage2D](https://developer.mozilla.org/en-US/docs/Web/API/WebGLRenderingContext/texImage2D)：视频纹理上传；本实现没有验证高位深/HDR画布输出，因此不用于这类源。
- [MediaCapabilities](https://developer.mozilla.org/en-US/docs/Web/API/MediaCapabilities/decodingInfo)：浏览器能力检查是配置相关的支持/流畅度/效率提示，不是画质还原认证。本轮采用实际解码尺寸和帧统计作为运行证据，没有伪造精确 codec 能力串。
- [CSS image-rendering](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/image-rendering)：high-quality 没有浏览器支持，不把无效 CSS 当作优化。

使用：刷新放映室，画面选择默认“原片 · 原生显示”；检查细节用“原像素 · 不放大”；符合条件且全屏放大时可比较“精细缩放 · SDR”。有损源或低分辨率源缺失的细节，播放器不能忠实恢复。当前极致保真策略仍是保留原码流/原色彩通路，并让额外缩放可选、可退回。
