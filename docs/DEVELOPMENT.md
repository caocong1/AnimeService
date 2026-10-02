# 开发与部署

## 代码入口

| 目录 / 文件 | 内容 |
| --- | --- |
| static/tokens.css、app.css、theme.js | 设计令牌（三个主题族 × 亮/暗）与全部组件样式；`theme.js` 在首帧前应用主题 |
| static/core.js | 共享：API、页头与底栏、格式化、封面、分集条与"票" |
| static/index.html、home.js | 追番：可以看的下一集、下载与首播、全部追番 |
| static/show.*、season.*、library.* | 作品页、新番（季度目录）、片库（文件与观看记录） |
| static/watch.* | 放映室：播放、标记看完与下一集、字幕与弹幕 |
| static/manage.* | 后台：状态、季度收录、清理、旧片库、设置（主题、下载偏好、设备） |
| anime/board.py | 首页与作品页的逐集状态（只读） |
| anime/app.py | HTTP 路由及 Host / Origin / 会话检查 |
| anime/db.py、engine.py | SQLite 状态、来源轮询、持久意图与对账 |
| anime/download_policy.py、thunder_bridge.py、qbit.py | 下载门禁与适配 |
| anime/webplayer.py、subtitles.py、dandan_history.py | 媒体、字幕、观看同步 |
| tests/ | 隔离测试 |

前端没有构建框架，编辑 static 文件后刷新即可。设计说明见 `.design/`（本地文件，不提交）。不要只打开 file:// HTML，页面依赖同源 API。播放器第三方文件通过 setup_web.py 安装。

## 开发配置

按 README 设置三个环境变量后用 uvicorn 启动：

- `ANIMESERVICE_DB`：默认 `data/anime.db`，开发时改为独立文件。
- `ANIMESERVICE_START_WORKER=0`：关闭 ASGI 应用的后台线程；不等于禁用所有手动 API 操作。
- `ANIMESERVICE_DEPLOYMENT`：默认 `data/deployment.json`；开发时指向独立文件或不存在的路径，避免读取生产访问设置。

测试 conftest 会在导入应用前设置独立临时数据库和配置路径。运行 `python -m pytest -q`。需要检查 JavaScript 时使用 `node --check static/core.js` 等；安装网页依赖后可运行 Node 代理范围测试。

## 部署与本机配置

这是 Windows 本机应用。当前存储适配仍约定新媒体 `D:/MediaLibrary/Anime`、未完成目录 `D:/MediaDownloads/Incomplete`；qB API 为 `127.0.0.1:4870`，账号文件为 `D:/MediaService/config/downloader-account.json`。不要为试用重置既有凭据、修改其他下载器任务或重建其配置。qB 的遗留独立配置启动路径仍在源码中，下载策略禁止在迅雷优先模式下使用它。

在新安装上，先用不启动后台的开发模式检查页面。正式接入需要逐作品确认来源、季集、旧文件及下载授权。早期空库的下载器缺省分支是 qB；迅雷生产接入必须明确保存 `downloader_preference=thunder_first`。仅开启 `thunder_ui_enabled` 不会安装界面执行器。季度自动下载与桌面守护也不能从别人的运行库复制授权。

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

`git clean -fdx` 会删除被忽略的数据，禁止在生产目录执行。不要从其他电脑复制测试数据库覆盖生产库。仓库没有自动部署或自动重启机制。

## 弹幕音频对齐

安装 requirements.txt 后，运行 `python scripts/setup_audio_alignment.py` 安装固定版本与 SHA-256 校验的 Windows fpcalc；setup_web.py 也包含此步骤。FFmpeg/ffprobe 使用已配置弹弹play的工具目录，或系统 PATH。缺少工具、访问受限或匹配不可靠时仍可手动调时。

第一版只处理公开可取音轨的 Bilibili UGC 来源。当前媒体必须已有核验的作品/集数，来源必须是系统匹配或用户明确选择的本集。POST `/api/web/media/{media}/alignment` 提交已登记的 source_id，立即返回后台状态；GET 同路径 `/{job_id}` 查询，POST `/{job_id}/cancel` 取消。系统不接受客户端传来的媒体路径或远端音轨 URL。

单并发后台读取本地音轨指纹和远端三个分散的音频片段；一致时使用 `本地时间 − 来源时间` 作为统一偏移。总时长差异不会用于拉伸时间或自动截尾。人工输入或提前/延后会停止自动应用；来源行内可关闭或重新自动对齐。设置按当前浏览器、媒体、来源保存。匹配成功缓存只保存数值指纹/结果，媒体变化会使缓存失效；不保存 PCM、签名 URL、Cookie 或上传本地媒体。巴哈姆特仍支持独立手动调时。

远端音轨每任务最多读取 8 MiB，通过仅监听回环、单来源的 Range 代理按实际读取量计数，同一任务内存复用已读片段；FFmpeg 的并行探测/跳转由最新读取请求接管。PCM 上限 80 MiB，本地时长限 4–60 分钟，三处匹配必须间隔足够且偏移差不超过 0.5 秒；后台期限约 120 秒。结果缓存 24 小时，本地指纹按路径/大小/修改时间/音轨/算法复用。

当前阈值经过控制实验和少量真实来源验证，尚未覆盖大量版本/配音/剪辑；失败时不猜偏移，中间剪辑和变速不支持统一自动对齐。测试全部使用临时媒体、假提取器和下载响应；`node --test tests/*.test.cjs` 检查人工优先与异步结果失效。

## 电视盒子客户端

`tv/` 是安卓盒子用的 APK（Kotlin、Compose、Media3），只做追番列表、作品分集和带自动弹幕的播放，接口与网页播放器相同。盒子不在家里时走现有的外网 HTTPS 地址（`public_authorities`），首次连接输入网页后台生成的配对码；盒子配对时声明 `device: tv`，会话有效期 365 天（其他设备仍为 30 天），「退出所有外部设备」同样立即作废。证书须由公共机构签发，App 内置 Let's Encrypt 根证书以兼容 Android 7.0 及更早的盒子。同一局域网内也可用 HTTP：设置 `bind_host`、`lan_authority`（盒子里填的 `IP:4871`）和 `lan_network`。

构建需要 JDK 17+ 和 Android SDK：`cd tv && ./gradlew publishRelease`，输出 `tv/app/build/publish/{fanyu-tv.apk,release.json}`。签名密钥 `tv/fanyu-tv-release.jks` 与 `tv/keystore.properties` 不进 Git，须另行备份；换了密钥，已装的盒子无法覆盖升级。

升级：每次发布先改 `tv/app/build.gradle.kts` 的 `appVersionCode`（递增）和 `appVersionName`，更新 `tv/release-notes.txt`，执行 `publishRelease`，再把两个输出文件复制到服务端 `data/tv/`。盒子启动时检查 `/api/tv/release`，有新版本就提示下载，校验 SHA-256 后交给系统安装；第一次升级需要在系统设置里允许番屿安装应用。
