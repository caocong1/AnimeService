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
