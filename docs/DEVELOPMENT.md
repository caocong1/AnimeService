# 开发与部署

## 代码入口

| 目录 / 文件 | 内容 |
| --- | --- |
| static/tokens.css、app.css、theme.js | 设计令牌（三个主题族 × 亮/暗）与全部组件样式；`theme.js` 在首帧前应用主题 |
| static/core.js | 共享：API、页头与底栏、格式化、封面、分集条与"票" |
| static/index.html、home.js | 追番：可以看的下一集、下载与首播；独立“全部追番”菜单 `/?view=all`，状态/搜索筛选及20/40/60部分页 |
| static/show.*、season.*、library.* | 作品页、新番（季度目录）、片库（文件与观看记录） |
| static/watch.* | 放映室：播放、标记看完与下一集、字幕与弹幕 |
| static/manage.* | 后台：状态、季度收录、清理、旧片库、设置（主题、下载偏好、设备） |
| anime/board.py | 首页与作品页的逐集状态（只读） |
| anime/show_list.py | 列表请求内的 SQLite 只读快照，批量读取分集、观看、放送、季度及播放进度 |
| anime/app.py | HTTP 路由及 Host / Origin / 会话检查 |
| anime/db.py、engine.py | SQLite 状态、来源轮询、持久意图与对账 |
| anime/download_policy.py、thunder_bridge.py、qbit.py | 下载门禁与适配 |
| anime/webplayer.py、subtitles.py、dandan_history.py | 媒体、字幕、观看同步 |
| anime/bangumi.py、bangumi_auth.py、bangumi_sync.py | Bangumi 公开 API、Windows 用户加密授权、本地到远端的持久个人记录同步 |
| tests/ | 隔离测试 |

前端没有构建框架，编辑 static 文件后刷新即可。设计说明见 `.design/`（本地文件，不提交）。不要只打开 file:// HTML，页面依赖同源 API。播放器第三方文件通过 setup_web.py 安装。

“全部追番”复用原 `GET /api/shows?scope=history` 只读快照，在浏览器对筛选结果分页；不是下载队列或观看状态写入。页码、每页数量、筛选与搜索保存在URL，筛选/搜索/改变页大小回第一页，超过末页的旧地址自动收敛。手机底栏为五个菜单，小屏分页保留上一页/当前页/下一页；桌面提供页码跳转。交互回归：`node --test tests/following-pagination.test.cjs`。静态更新不需要重启后台。

## 开发配置

按 README 设置三个环境变量后用 uvicorn 启动：

- `ANIMESERVICE_DB`：默认 `data/anime.db`，开发时改为独立文件。
- `ANIMESERVICE_START_WORKER=0`：关闭 ASGI 应用的后台线程；不等于禁用所有手动 API 操作。
- `ANIMESERVICE_DEPLOYMENT`：默认 `data/deployment.json`；开发时指向独立文件或不存在的路径，避免读取生产访问设置。

测试 conftest 会在导入应用前设置独立临时数据库和配置路径。运行 `python -m pytest -q`。需要检查 JavaScript 时使用 `node --check static/core.js` 等；安装网页依赖后可运行 Node 代理范围测试。

`GET /api/shows` 使用单次请求内的只读事务和分批绑定参数（每批最多 400 项），避免逐作品重复打开连接。首页与历史列表复用相同分集、放送及观看数据，只对符合原下载门禁的作品计算缺集；作品详情及后台下载仍使用原完整缺集计算。没有跨请求列表缓存，文件大小、观看修正及播放进度在下一次请求重新核验。性能回归见 `tests/test_show_list_performance.py`，覆盖与详情分集状态一致、累计编号、边界状态、状态保持及 405 部列表查询成本。

Bangumi 同步入口 `/manage#bangumi`，GET `/api/bangumi-sync` 只返回非秘密账号、设置与逐作品队列状态。POST 同前缀的 `/connect`、`/settings`、`/disconnect`、`/retry` 均限本机回环访问且保留同源会话 token 检查。连接和启用分两步，空库默认关闭；连接核验 `/v0/me`，凭据只保存到数据库同目录 DPAPI 加密的 `bangumi-auth.bin`，不进入 kv。测试必须使用 MemoryVault/FakeBangumi，不读取生产授权或向真实账号写入。背景线程每 5 秒处理 selected=1 的变更队列；SQL 触发器随个人状态及 watches.finished 变更原子入队，媒体/目录更新不生成已看。回执按账号隔离；先持久意图、回读、最小字段写入、再回读，未知提交不盲目重发。手动观看修正、外部冲突、跨季章节映射和保护字段隔离见本机研究说明 `docs/BANGUMI-PLATFORM.md`；平台说明已纳入文档目录；账号、原始同步回执和本机证据保留私有。

季度搜索按响应实际条数推进 offset。Bangumi 的实验性搜索接口可能把请求的 limit 限制为更小的值，不能按请求条数推进。同步按唯一作品 ID 核对 total；分页提前为空、偏移不符或重复导致覆盖不足时保留旧成功时间并报告不完整。`catalog_<季度>` 中的 `count`/`total`/`pages`/`complete` 记录本次分页覆盖情况，包含所有动画类型；页面再按日本 TV/WEB 等规则筛选。普通目录同步只更新资料，不改变追番、观看、源映射或下载授权。

## 部署与本机配置

这是 Windows 本机应用。当前存储适配仍约定新媒体 `D:/MediaLibrary/Anime`、未完成目录 `D:/MediaDownloads/Incomplete`；qB API 为 `127.0.0.1:4870`，账号文件为 `D:/MediaService/config/downloader-account.json`。不要为试用重置既有凭据、修改其他下载器任务或重建其配置。现有部署采用 qB 后台优先，禁止因离线自动启动或切换实例；源码保留的遗留启动分支不构成执行授权。

在新安装上，先用不启动后台的开发模式检查页面。正式接入需要逐作品确认来源、季集、旧文件及下载授权。空库下载器缺省分支是 qB；当前项目 `thunder_ui_enabled=false`，不运行定时桌面提交。历史迅雷能力仅在另行明确授权且完成任务核对后使用，仅开启开关不会安装界面执行器。季度自动下载与桌面守护也不能从别人的运行库复制授权。

`docs/deployment.example.json` 可复制到 `data/deployment.json`。缺省只监听 `127.0.0.1`。可配置字段：

| 字段 | 说明 |
| --- | --- |
| bind_host | anime.run 的监听地址，缺省 127.0.0.1 |
| public_authorities | 允许的 HTTPS Host（包含实际端口）；仍需设备配对 |
| lan_authority、lan_network | 可选 HTTP LAN Host 和来源 CIDR；显式启用后该网段无需配对，保留写操作 token / Origin 校验 |
| certificate | 现有本机 HTTPS 适配器的证书路径；仅用于已有部署 |

仓库不提供 DNS 凭据、反向代理配置、证书签发或路由器映射脚本。已有部署的 edge 模块只有检测到本机证书、配置和程序后才会运行；自动续期与端口映射属于本机保留脚本。新部署自行配置可信 HTTPS 反向代理，代理头仅信任回环地址。

迅雷 UI 执行遵循持久队列的领取、界面核对、preflight、单次提交及台账回读；锁屏等待解锁，不绕过认证。无法确认失败任务已停止时不能改用 qB。网页浏览、收藏或播放不代表完成这些步骤。

## 更新已有部署

1. 检查 `git status`，保存本机代码修改；备份 data 和本机配置。运行中数据库使用 SQLite online backup，或者停服务后复制完整数据目录。
2. 检查待合并变更并运行隔离测试。
3. `git pull --ff-only`，依赖变更时更新依赖；需要网页依赖时重跑 setup_web.py。
4. 在合适时间重启本项目服务并检查入口、后台状态；不重启或切换其他下载器。

`git clean -fdx` 会删除被忽略的数据，禁止在生产目录执行。不要从其他电脑复制测试数据库覆盖生产库。仓库没有自动部署机制；`scripts/supervisor.py` 只负责本机进程：子进程退出后按 5 秒起步、最长 5 分钟的退避重启，子进程存活期间每 30 秒探测 `/api/health`，连续 6 次失败视为挂死并重启；`data/stop.request` 出现时先让 `anime.run` 自行退出（它同样监视该文件），30 秒后才强制结束；`logs/supervisor.log` 与 `logs/process.log` 超过 5 MiB 保留一代轮转。探测地址取 `deployment.json` 的 `bind_host`。正在停止时再次启动会等待旧 supervisor 退出后接管，不会静默失败。

`GET /api/health` 只读 kv，不扫描表：返回服务名、`ok`（心跳不超过 max(600 秒, 3×poll_seconds)）、心跳阶段与年龄、维护循环年龄、下载器与字幕工坊健康、`cycle_errors`（调度各步最近一次失败的错误、次数与起始时间）、进程 pid、启动时间与代码版本（`git rev-parse --short HEAD`）。`/api/status` 仍是详细视图。调度循环里的每一步（来源登记、选源、字幕工坊 discover/tick 等）各自隔离：一步失败只记入 `cycle_errors` 并在第 1、5、20、100 次写事件，其余步骤与空闲心跳照常。

## 浏览器兼容转封装缓存

多 `mdat` 的 MP4 会按需做仅复制封包的转封装，缓存在数据库同目录 `cache/browser-media/`，按文件指纹命名并附校验 JSON。转封装失败时临时 `.partial.mp4` 一律清除并返回「浏览器兼容转封装失败」；缓存总量超过 `kv browser_media_cache_bytes`（默认 8 GiB）时按最后修改时间淘汰最旧的副本（刚生成和正在生成的保留），原片永不改动。弹弹play 历史导入在播放器数据库文件大小与修改时间都未变化时跳过复制与 PowerShell 读取，健康记录带 `unchanged`。

## 播放进度写入契约

`POST /api/web/media/{key}/progress` 接受 `position`、`duration`、`playing`，可选 `session`（16–80 位 `[A-Za-z0-9_-]`）与 `sequence`（≥1 的整数）。两者同时给出时，服务端按会话记录最新序号：同一会话的迟到或重复序号、以及该会话已绑定到其它媒体时，返回 `{"ok":true,"ignored":true}` 且不改位置；成品（`finished:` 前缀）必须给出这对字段，普通媒体可省略以兼容旧客户端和当前 TV 端。网页端对普通媒体同样发送会话序号并串行发送；TV 端接入成品/续播契约时应同样携带。任何进度写入都不会产生已看记录。

## 网页音轨选择

放映室在字幕上方提供「音轨」，播放器设置中的同名列表支持全屏使用。音轨和字幕各自仅在实际轨道数大于 1 时显示选择行（不计文件默认/关闭选项）；设置中的音轨入口同样仅多轨显示，单轨字幕继续默认加载。按 ffprobe 实际流索引、语言、标题、编码和声道列出全部音轨；未知语言明示、重复语言以轨号和标题区分，不凭语言标记推断原生配音。文件默认保留原文件；明确选择后，播放进度、暂停/播放、音量、静音、倍速、字幕和弹幕保留。浏览器按媒体指纹保存成功的选择，媒体变化后失效；切换不写已看记录。

GET `/api/web/media/{key}/audio-tracks` 只探测登记文件；POST `/{index}/prepare` 受原同源 token 校验，单并发后台准备；GET `/{index}` 查状态，GET `/{index}/stream` 仅返回完成缓存，支持 Range。所有地址均在上述前缀下，不接受客户端媒体路径。H.264/HEVC/AV1 视频直接复制；AAC/MP3 音频直接复制，其它音频转为 AAC，HEVC 使用 hvc1 标签；不重新编码画面、不修改原媒体。浏览器仍需支持原视频编码，失败回退原播放并保留本机播放器。

派生文件仅在数据库同目录的 `cache/audio-tracks/`，按路径/大小/mtime 指纹及音轨索引缓存，`.part.mp4` 完成并核实时长后才原子发布。缓存上限 8 GiB，准备前按源大小及音频预算检查空间并保留 256 MiB 余量；仅淘汰此目录中的生成文件，片库和下载目录不变。准备最长 20 分钟、单并发，重复同轨请求幂等，失败可显式重试；正常关闭服务会终止准备进程。不要在有准备进程时强制结束服务。首选轨没有准备完成时原播放可继续，失败不保存新选择。

`tests/test_audio_tracks.py` 覆盖实际流索引、默认标记、媒体变化失效、并发/空间限制、失败清理、逐轨映射、只复制视频、鉴权及 Range；真实 FFmpeg 双轨 fixture 和 Edge 浏览器复现脚本/截图保存在本机 docs，所有测试观看写入均拦截。前端 ArtPlayer 5.4.0 通过原生 video.paused 判断暂停，切源 loadedmetadata 不重复执行首开自动播放或旧断点恢复。

## 弹幕音频对齐

添加来源支持作品/视频标题搜索，以及独立的「B站网址 / BV号」直达查询。普通搜索框也兼容 BV号和标准 B站视频页。GET `/api/web/danmu/resolve?q=...` 通过固定视频元数据接口获取真实标题、分P、时长，网址只保留公开 BV和正整数 p；不请求用户传入的主机，不将任意查询参数发送到上游。当前直达支持普通 BV投稿视频，包括 www/m/无www 的 bilibili.com 视频页；不解析短链接。

B站搜索结果保留上游自带的 UP主和「视频总弹幕」字段，不额外逐项请求。选中分集后 GET `/api/web/danmu/source/{source_key}/info` 只读取已登记来源的元数据：普通 BV通过视频页查询核验分P/cid/UP主，官方番剧通过固定 season 接口核验 ep_id/cid，再读取弹幕元数据 protobuf 的 count；不请求视频或整份弹幕。这个数字是原站当前 cid 的弹幕池数量，与加载后经过过滤/去重的实际条数可能不同。真零显示「0条·暂无弹幕」，受限、关闭、无效或失败显示「数量暂不可用」，不伪造0。成功内存缓存10分钟（最多256项）、弹幕数量失败退避30秒；前端300ms防抖、单并发并只接续最后选中的分P，旧结果不覆盖新选择。添加不等数量查询，仍由用户显式确认本集；窗口保持打开，同来源显示已添加并阻止重复，保留最多5个来源。UP主跟随已选来源在本机浏览器保存，不改变作品季集匹配、观看/下载状态。

B站 UGC标题搜索保留原查询，最多追加两次去版本/标点、去季集的查询，按完整核心关键词过滤无关推荐并按 BV去重；手动结果保留原上传标题，不用资料库标题替换，也不因缺季号丢掉分P候选。搜索是否能找到视频仍受 B站索引限制；标题与作品名不同或未收录时使用直达。UGC分P不推定作品集号，查询仅登记稳定来源，用户确认「添加」后才加载。不会更改自动匹配的完整作品/季集核验或下载、观看状态。

安装 requirements.txt 后，运行 `python scripts/setup_audio_alignment.py` 安装固定版本与 SHA-256 校验的 Windows fpcalc；setup_web.py 也包含此步骤。FFmpeg/ffprobe 使用已配置弹弹play的工具目录，或系统 PATH。缺少工具、访问受限或匹配不可靠时仍可手动调时。

自动对轴处理公开可取音轨的 Bilibili UGC 与巴哈姆特来源。GET `/api/web/alignment-capabilities` 返回当前后端实际支持的平台；旧后端仍运行时巴哈按钮显示等待空闲更新，不把静态按钮冒充后端已加载。当前媒体必须已有核验的作品/集数，来源必须是系统匹配或用户明确选择的本集。POST `/api/web/media/{media}/alignment` 提交已登记的 source_id，立即返回后台状态；GET 同路径 `/{job_id}` 查询，POST `/{job_id}/cancel` 取消。系统不接受客户端传来的媒体路径或远端音轨 URL。

单并发后台读取本地音轨指纹和远端三个分散的音频片段；一致时使用 `本地时间 − 来源时间` 作为统一偏移。总时长差异不会用于拉伸时间或自动截尾。人工输入或提前/延后会停止自动应用；来源行内可关闭或重新自动对齐。设置按当前浏览器、媒体、来源保存。匹配成功缓存只保存数值指纹/结果，媒体变化会使缓存失效；不保存 PCM、签名 URL、Cookie 或上传本地媒体。

巴哈姆特经固定匿名 device/token/video_src 接口读取正常访问授权并取得 HLS，选最低带宽的有限 VOD，仅取约22%/48%/75%位置各35秒的 TS 音频；分片、播放清单、密钥合计限制8MiB。仅允许已知巴哈CDN和公开DNS，已配置的本机HTTP代理允许198.18/15假DNS路由标记，由代理解析CONNECT主机，其它私网仍禁止；不改变代理或系统网络。复用现有yt-dlp纯Python AES-128处理常规HLS分片，不新增依赖；DRM/SAMPLE-AES、未知格式、跨不连续点、取流失败或三个锚点不一致均不应用偏移。只使用匿名正常取流，不导入账户Cookie、不绕过广告/权限；token未授予播放时停止，不伪造广告完成或提交广告统计。原站1018异常访问限制触发进程内30分钟冷却，停止重试且保留原偏移。

2026-10-16验证：生成音频预设12秒偏移，经实际FFmpeg/AAC/HLS/fpcalc路径测得11.915秒，三个锚点一致；这是合成技术证据。真实已登记巴哈51083在既有本机代理下返回1018，直连超时，未获得真实巴哈音轨匹配结果。证据 `data/diagnostics/bahamut-alignment-20261016/`；部署助手 `scripts/deploy_bahamut_alignment.py` 等待播放租约、最近进度、桌面播放器及后台轮次均空闲后最多重载一次，随后只读核验能力端点；实际加载状态以其中deployment.json为准。

远端音轨每任务最多读取 8 MiB，通过仅监听回环、单来源的 Range 代理按实际读取量计数，同一任务内存复用已读片段；FFmpeg 的并行探测/跳转由最新读取请求接管。PCM 上限 80 MiB，本地时长限 4–60 分钟，三处匹配必须间隔足够且偏移差不超过 0.5 秒；后台期限约 120 秒。结果缓存 24 小时，本地指纹按路径/大小/修改时间/音轨/算法复用。

当前阈值经过控制实验和少量真实来源验证，尚未覆盖大量版本/配音/剪辑；失败时不猜偏移，中间剪辑和变速不支持统一自动对齐。测试全部使用临时媒体、假提取器和下载响应；`node --test tests/*.test.cjs` 检查人工优先与异步结果失效。

对齐失败返回固定的 `reason`、安全提示及 `retryable`，区分依赖/工具、本地文件/音轨、来源信息/取样、时长、指纹、流量上限和超时。前端仅对可重试的临时失败及网络/429/5xx、已获得job_id后服务重启导致的任务404，在5秒和15秒后各追加一次尝试；新尝试使用force跳过短期失败结果缓存，并保留本地指纹缓存。初次提交的媒体404不重试。每次任务仍受原120秒与8 MiB限制，持续失败保留现有偏移，可手动重新对齐；手动输入、关闭、移除或离开页面停止旧尝试。不会为缺少工具、无独立音轨、短来源、读取超限或不可靠匹配自动重试，也不输出工具stderr、私密URL或凭据。

## 电视盒子客户端

`tv/` 是安卓盒子用的 APK（Kotlin、Compose、Media3），只做追番列表、作品分集和带自动弹幕的播放，接口与网页播放器相同。盒子不在家里时走现有的外网 HTTPS 地址（`public_authorities`），首次连接输入网页后台生成的配对码；盒子配对时声明 `device: tv`，会话有效期 365 天（其他设备仍为 30 天），「退出所有外部设备」同样立即作废。证书须由公共机构签发，App 内置 Let's Encrypt 根证书以兼容 Android 7.0 及更早的盒子。同一局域网内也可用 HTTP：设置 `bind_host`、`lan_authority`（盒子里填的 `IP:4871`）和 `lan_network`。

构建需要 JDK 17+ 和 Android SDK：`cd tv && ./gradlew publishRelease`，输出 `tv/app/build/publish/{fanyu-tv.apk,release.json}`。签名密钥 `tv/fanyu-tv-release.jks` 与 `tv/keystore.properties` 不进 Git，须另行备份；换了密钥，已装的盒子无法覆盖升级。

升级：每次发布先改 `tv/app/build.gradle.kts` 的 `appVersionCode`（递增）和 `appVersionName`，更新 `tv/release-notes.txt`，执行 `publishRelease`，再把两个输出文件复制到服务端 `data/tv/`。盒子启动时检查 `/api/tv/release`，有新版本就提示下载，校验 SHA-256 后交给系统安装；第一次升级需要在系统设置里允许番屿安装应用。

播放器自适应缓冲（1.0.2）：`Runtime.maxMemory()` 的八分之一为压缩媒体字节目标上限，范围 2–32 MiB；达到字节目标优先停止加载，并允许以已有非空缓冲起播，避免高码率文件等待无法装入内存的时间目标。App 堆使用达到 75% 时目标降为原来的一半（最低 2 MiB）；堆使用低于 60% 持续 30 秒后才恢复。该目标是媒体分配器的加载阈值，不是整个 App/进程的内存硬上限，加载批次可能略微超出；MP4 索引、解码器、封面及弹幕另占内存。

`PlaybackTransferEstimator` 仅采集播放器数据源的网络读字节和时间；暂停加载时结束测量窗口，恢复时重新开始，排除缓冲已满后故意不下载的时间。不采集 API/封面/弹幕请求，不保存媒体地址、文件路径或设备标识。小于 64 KiB 或 100 毫秒的样本不参与策略；吞吐平滑、波动估计和至少 3 个有效样本共同决定策略。影片平均总码率来自服务返回的已核验文件大小与播放器实际时长，无需新增服务器探测或改写片库。

未知网络保持 1.0.1 的最低/最高 10/30 秒、起播/重缓冲 2.5/5 秒；持续稳定且吞吐至少为影片码率的 3 倍时转为 8/15 秒、1/3 秒；普通网络为 10/25 秒、2.5/5 秒；弱/波动网络为 15/30 秒、4/8 秒。降低等待需稳定观察 30 秒，真实缓冲耗尽后保守策略保持至少 120 秒；Media3 `lastRebufferRealtimeMs` 区分快进/初次缓冲和真正耗尽，重复回调不延长同一事件。实际剩余缓冲决定加载迟滞，播放速度按实际播放时间折算。超过 60 秒无有效测速时回到未知策略；上述窗口始终受字节目标限制。

`AdaptiveLoadControl` 使用 Media3 原分配器和准备/停止/释放生命周期，只覆盖加载/起播决策；平台解码器和初始化失败回退保留。`FanyuPlayback` 仅记录策略名、阈值、堆上限、错误码和异常类型，不输出异常消息、堆栈、媒体 URL 或请求头。现有服务仍直传单个原文件，没有自动转码/降低码率；持续带宽不足或硬件不支持无法仅靠缓冲解决。`./gradlew :app:testReleaseUnitTest :app:publishRelease` 验证策略及播放器适配后构建正式同签名升级包。

接口核对使用 [Media3 1.11.1 LoadControl](https://github.com/androidx/media/blob/1.11.1/libraries/exoplayer/src/main/java/androidx/media3/exoplayer/LoadControl.java)、[DefaultLoadControl](https://github.com/androidx/media/blob/1.11.1/libraries/exoplayer/src/main/java/androidx/media3/exoplayer/DefaultLoadControl.java) 和本机固定版本 AAR；不根据未来版本接口修改本项目依赖。

1.0.3 修复初始化回归：不能用 Kotlin `LoadControl by delegate` 假设 Java default 方法也被转发。该编译器只为抽象接口方法生成转发；Media3 的 `getBackBufferDurationUs`、`retainBackBufferFromKeyframe`、`onTracksSelected` 等默认实现会调用旧方法或直接抛异常。适配层显式实现所有当前非弃用接口；不在此单播放器控制器中启用额外预加载。JUnit 覆盖构造期间的读取、轨道回调和当前接口完整性，必须先在修复前复现故障，再验证修复。

正式 Android 启动/播放验证：`./gradlew :app:assembleReleaseAndroidTest` 生成同签名测试 APK；仅在专用模拟器中安装 release 和 androidTest APK，然后运行 `adb -s <模拟器序列号> shell am instrument -w com.fanyu.tv.test/com.fanyu.tv.player.PlayerStartupInstrumentation`，需看到 `PASS: signed release`。测试使用 androidTest/assets 中自产的 30 秒 AVC/AAC 色块/正弦音视频和进程内回环 HTTP 假服务，依次打开两个测试条目、用遥控右键快进、核对实际播放后的进度提交再关闭；所有进度/API写入只进入假服务。不能把测试服务地址或测试 APK 安装到用户盒子，也不能将模拟器验证写成 Z9X 固件实测。测试逻辑用平台 Java API，通过正常进度接口验收，避免引用 release 中可能被 R8 删除或改名的方法；测试素材不进入正式 APK。

`res/raw/isrg_root_x1.crt`、`isrg_root_x2.crt` 是网络配置原已引用的公开根证书，使用 Let's Encrypt [官方 DER 文件](https://letsencrypt.org/certificates/)，不含私钥；不应被私有 `.pem` 忽略规则漏掉。SHA-256 分别为 `96BCEC06264976F37460779ACF28C5A7CFE8A3C0AAE11A8FFCEE05C0BDDF08C6`、`69729B8E15A86EFC177A57AFB7171DFC64ADD28C2FCA8CF1507E34453CCB1470`。

## 设备配对与重启

设备凭据以摘要和到期时间保存在SQLite的remote_sessions中，浏览器保存HttpOnly/Secure/SameSite=Strict Cookie；普通网页30天、电视盒子365天。Cookie同时带Max-Age和Expires。服务重启仅更新防跨站写入token，不能重置设备凭据；本轮未延长或改写既有设备会话。

已配对设备访问/login时直接返回首页。由其他网站链接进入时，Strict Cookie可能不随首个导航发送；登录页通过同源、no-store的GET /api/bootstrap重新核验已有Cookie，成功才进入首页，失败保留手动配对。不得把Cookie/配对码/会话token存入URL或localStorage；显式注销、撤销及过期仍生效。

回归：tests/test_remote_ui.py验证同一临时数据库上的应用与浏览器重建、过期/撤销/错误Cookie拒绝；node --test tests/login-pairing.test.cjs验证自动恢复、服务不可用及手动配对异步竞争。旧浏览器网页仍持有重启前的防跨站token时需刷新页面获取新token，这不等于设备配对失效。

## 首集选源评测

`anime/subtitle_history.py`把字幕实评快照追加到`subtitle_review_history`，`record_review`在同一事务更新当前值和历史，避免覆盖或部分保存。`archive_existing`补存既有评审且幂等，记录键按内容+前一版本链接，支持A→B→A。`report(latest=True)`统计最新候选，完整版本JSON可导出；不要按修订数累计独立评测。显式中文软字幕规范化文本哈希只在评审/分析指纹一致时读取，同作同集关联相同文本；不用OCR/日语汉字猜译稿或译者归属。未结构化findings与未核实身份留未知。

页面`/subtitle-reviews`沿用现有样式，支持作品过滤、历史查看和JSON导出；作品选源区有入口。同源GET访问规则/POST令牌门禁保留，不能在测试中提交生产评审；隔离回归`tests/test_subtitle_history.py`覆盖原值回填、修订/回退、幂等、事务回滚、指纹/中文身份、无中文不计零分及API门禁。实际整理的findings和导出数据是本机私有记录，不入Git。

`anime/fidelity.py`识别已声明的超分/补帧/AI增强，`source_fidelity=original`用于标题及已选manifest文件名的资格门禁；不按分辨率/编码/帧率猜加工。已有AI实评分和文件保留，推荐/主备过滤。迅雷取消仅针对实际无任务、无确认/租约/committing的意图；已确认和不确定提交继续核查，preflight再读当前偏好。测试`tests/test_fidelity.py`用临时数据库、假下载器验证入队/取样/最终点击、候选替补、历史保留和明确首集切换。

choose的`replace_first_hash`默认空，普通选择已有首集时不替换；只有具体人类明确指令才可传旧hash，核验旧/新独立首集及本项目迅雷归属，保留文件和观看、完成时间，并保存旧分集审计快照。自动评审不得使用此参数。

`source_review.py` 使用 `source_policies`、`source_samples` 和 `tasks.purpose=source_sample`，将首集多版本研究与正片/观看分开。每作品上限12种片源、20GiB，软/硬字幕与不同平台、分辨率分组，同编码简繁合并。取样只通过既有下载门禁及迅雷持久队列；弃番、暂停、完结、清理保留或首集不在授权范围时拒绝。`source_review_new_shows` 开启后，新季度独立核验授权进入sampling；普通资料同步不改用户追番状态。

完成样本后台串行提取ffprobe、文字字幕VTT/原ASS、六个原尺寸PNG。硬字幕复用已安装Windows中文OCR，通过运行依赖 `scripts/Ocr-SourceFrames.ps1`；不安装语言或上传云端，图形字幕overlay、上方字幕、短句及OCR错字仍需人工核对。OCR时间为采样估计值。宽高、码率、可读速度是事实，不自动成为画质或翻译分；两项实评需要证据、日语/原作依据、评审者及中/高置信度。没有内置大模型/日语ASR或收费API。

作品页 `/show/<id>#source-review` 展示证据、评审和主源/最多4个替补。`/api/shows/<id>/source-review` 下的start/review/choose仍使用原同源写入门禁。主源确认时完成首集硬链接归库，不重下；只有本集主源候选缺失且逾等待窗口才按已评审替补顺序补缺，已入库集不替换。补查不会重置已选主源，文件指纹变化需重新评审。生产实评原始台账与私有操作资料不进Git；回归 `tests/test_source_review.py` 全部使用临时数据库及假下载器。

`review.decision=excluded` 用于实际无中文字幕等资格失败，须证据/来源/评审者，不产生虚构画质或翻译分、不进入推荐。当前主源/替补需先换掉才允许排除。`subtitle_metrics` 将日文汉字计入han_cues但不当中文。已知airing_epoch的等待从播出起算，无时刻从首次同集发现起算，避免延迟发现替补再次重置等待。实际4K16位PNG可大于32MiB，读取上限64MiB。

迅雷claim可选show_id过滤本次队列；全局lease仍互斥。最终提交后error先以真实回执对账，无法确认则单项review释放全局lease，保留committing证据禁止重投；有真实ack则保留下载/完成状态。主源已有任务而下载失败/所有权不确定时不另开替补版。定时Codex实评接续应独立于既有UI提交执行器，不并发claim；只有实评结果才保存主/备政策，不能把任务已创建当全量评审完成。


## 新番发现（2026-10-06）

/season 默认全部年份的已收录动画，支持 TV、WEB、剧场版、OVA/OAD、已核验跨季续播与可选年份/季度；旧 q=2026-10 和 kind=series/all/uncertain 链接兼容。完整首播日期显示在卡片，最新/最早/评分排序只影响视图。

GET /api/catalog/discover 接受 q、kind、year、offset、include_unknown。联网结果只读、不 upsert、不 prepare、不新增来源或授权。Bangumi 每页实际返回数量决定 next_offset，类型/地区在每页过滤；排序仅作用于已加载结果，不声称全站已完整收录。无需关键词也可使用 cat=3/2 浏览剧场版/OVA。

POST /api/catalog/import/{sid} 校验本机会话/同源及动画条目后只保存资料，quarter 留空，保留原 selected/state/authorized/auto_download/cleanup_hold/观看/映射。详情点击才收录，想看另调用既有显式 state 操作。普通季度下载与多版本评审门禁不改。

	ests/test_discovery.py 使用临时库与 Mock 验证跨季度筛选、实际分页、资料导入保护、失败/未知地区/非法条件；不运行生产下载器。


## 项目知识与提交

长期文档入口为 [文档目录](README.md)、[当前规则](PROJECT-RULES.md)、[项目经验](PROJECT-EXPERIENCE.md) 和 [任务方式](AUTOMATION.md)。旧章节包含历史实现，下载器优先序、字幕首版门槛、两阶段版本、已看保护及周期以当前规则为准。

字幕控制入口 `scripts/subtitle_delivery_queue.py` 与 `scripts/viewing_queue.py` 纳入仓库；`finish_subtitle_episode.py`/`publish_finished_episode.py` 负责不可变成品与索引，`anime/finished_viewing.py` 负责服务端进度与已看。`static/finished-releases.js` 将成品合并到首页/全部追番/作品分集；相关隔离覆盖在 `test_finished_viewing.py`、`test_subtitle_two_pass.py` 和 `finished-releases.test.cjs`。

`pytest.ini` 明确默认收集 tests 与 audio_review_lab，排除本机 data/research 中第三方测试树。Git 保存范围和仍需本机备份的内容见 [Git 整理](GIT-ARCHIVE.md)。
