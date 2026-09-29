# 番屿 · AnimeService

本机优先的动画追番、片库与观看记录管理工具。Python / FastAPI / SQLite 后台，原生 HTML、CSS、JavaScript 网页界面。

> 当前是从个人 Windows 部署整理的早期项目，不是开箱即用的跨平台下载器。网页和核心测试可独立开发；迅雷、弹弹play和后台自启依赖 Windows，部分存储路径仍有固定约定。

## 功能

- 季度目录、作品详情、想看 / 试看 / 在追 / 暂搁 / 弃番 / 看完；下载与观看状态分别保存。
- 来源抓取、作品与季集核验、重复版本去重、持久下载意图与缺集检查。
- 迅雷持久界面队列及完成对账；qBittorrent 备用路径受下载策略和任务所有权约束。
- 本地媒体网页播放、弹幕、播放进度及文本字幕支持；格式兼容性有限，保留弹弹play入口。
- 弹弹play历史单向同步：只有已核验的 `LastWatched` 才计入已看，手动修正优先。
- 旧片库只读关联；清理候选须在页面核对具体文件后确认，历史保留。
- 多主题界面；可选 HTTPS 设备配对访问。

## 快速开始：开发网页

需要 Python 3.11+、Git。网页播放器依赖安装另需 Node.js / npm。当前本机验证环境为 Windows、Python 3.14；其他环境的桌面集成未验收。

```powershell
git clone https://github.com/caocong1/AnimeService.git
cd AnimeService
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
$env:ANIMESERVICE_START_WORKER = '0'
$env:ANIMESERVICE_DB = Join-Path $PWD 'data/dev/anime.db'
$env:ANIMESERVICE_DEPLOYMENT = Join-Path $PWD 'data/dev/deployment.json'
python -m uvicorn anime.app:app --host 127.0.0.1 --port 4871
```

打开 <http://127.0.0.1:4871>。新库为空；可从页面同步公开作品资料。上述方式关闭后台下载、观看历史同步和桌面守护线程，但页面的手动操作仍有效。开发时不要配置真实下载器、关联真实媒体或授权下载。已有服务占用 4871 时，应在另一份独立环境开发，不能启动第二个生产实例。

macOS / Linux 使用 `source .venv/bin/activate`，将三个变量改用 `export NAME=value`。Windows 桌面适配与安装脚本不能在这些平台使用。

可选安装网页播放器和独立弹幕适配器：

```powershell
python scripts/setup_web.py
```

该脚本下载固定版本的第三方依赖，不携带账号或媒体；开发模式不自动启动弹幕服务。依赖许可见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 测试

```powershell
python -m pytest -q
```

测试使用临时数据库和假下载器，不启动生产后台。安装可选网页依赖后，可运行 `node --test tests/bahamut-proxy.test.mjs`。更多代码入口、开发与更新方法见 [开发文档](docs/DEVELOPMENT.md) 和 [贡献说明](CONTRIBUTING.md)。

## Windows 实际部署

请先阅读 [部署限制](docs/DEVELOPMENT.md#部署与本机配置)。确认本机路径与下载器所有权后，`Install-AnimeService.ps1` 会安装 Python 依赖、创建当前用户登录自启和桌面快捷方式，并立即启动后台；它不是仅安装依赖的命令。

- `Start-AnimeService.ps1` / `Stop-AnimeService.ps1`：启动或停止本项目服务。
- `Remove-Autostart.ps1`：仅移除本项目登录自启。
- 运行数据保存在 `data/`，日志保存在 `logs/`，都不提交 Git。
- 新安装默认仅监听本机；其他访问入口需要单独配置。

迅雷提交依赖另行安排的界面执行器、客户端和已解锁桌面，克隆仓库不会自动创建定时任务。qB 失败回退没有完整自动交接，不能宣称已实现无缝切换。MKV / HEVC / 内嵌字幕仍受浏览器和本机工具限制，没有通用转码。

## 开源范围

仓库包含源码、隔离测试、依赖清单与通用文档。数据库、私人 RSS、凭据、证书、观看历史、媒体、截图、本机运维脚本及历史交接记录均不包含。根目录 `.gitignore` 使用公开文件白名单，新添根文件需要显式加入。

项目原创代码按 [MIT](LICENSE) 发布；第三方组件保留各自许可证。仅使用你有权访问和使用的媒体与来源。本项目不提供媒体内容或账号。
