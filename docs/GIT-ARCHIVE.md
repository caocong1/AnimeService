# Git 整理与保存范围

2026-10-15 按用户“本项目都整理提交 Git，包括字幕心得、定时任务方式”的要求整理。此轮是本地版本提交，不发布媒体、不推送远端、不改运行任务或业务数据库。

## 纳入仓库

- 当前应用、网页、Android TV、隔离测试及通用脚本的既有未提交改动。
- 字幕制作/交付/细调、原声研究、字幕库、观看、选源、集数、画面及账号同步的长期说明。
- 当前规则、项目经验、定时任务分工与脱敏提示词模板；必要队列 CLI、守卫源码和非秘密项目上下文配置。
- Python 依赖锁定快照。它记录当前 Windows 环境，不意味着所有可选原声模型环境、Android SDK 或第三方工具已随仓库提供。

`.gitignore` 保持显式白名单。新增长期文档和脚本须明确加入；不能为方便 `git add` 删除保护或强制加入整个 data/。

## 本机保留

| 内容 | 保存位置与理由 |
| --- | --- |
| DB、观看、授权、私人 RSS、调度台账 | data/；实时个人数据和 owner/lease 不能靠克隆复制 |
| 原片、字幕正文、歌词、截图、字体包和成品 | 媒体目录、data/subtitle-studio/、static/finished-media/；私有素材及大文件不公开 |
| 模型/依赖环境、缓存、日志、APK 与签名 | .venv/、vendor/、tools/、logs/、tv/build/、本机密钥；可再生成或敏感 |
| 真实 automation.toml、回执和原始会话 | 用户 Codex 目录与 data/diagnostics/；只提交脱敏模板和实现 |
| 原始 AGENTS/PROGRESS/HANDOFF/DECISIONS/REQUIREMENTS | 项目根本机历史；当前可复用规则已汇总，不把长会话日志逐字公开 |
| SOURCE-REVIEW、ACCEPTANCE、THUNDER-INTEGRATION、UI-REDESIGN 历史记录 | docs/ 中原文件保留；含私人逐次实评、部署地址或过时运行快照，提炼原则进入文档目录 |
| 一次性 apply/reload/探测/浏览器核验脚本 | 原 scripts/ 或 docs/；可能写真实库或绑定具体生产实例，不作为通用安装入口 |

这里的“本机保留”不意味着 Git 可恢复这些文件；运行数据仍需单独备份。生产目录禁止 `git clean -fdx`，不要用 checkout/reset 覆盖并发制作中的文件。

## 验证

`pytest.ini` 将默认收集限制在 `tests/` 和 `audio_review_lab/`，避免把 data/research/ 内第三方项目的测试当成本项目测试。`tests/conftest.py` 在应用导入前设置临时库和关闭后台；专项测试再用假下载器/假账号。

```powershell
.venv/Scripts/python.exe -m pytest -q
node --test tests/*.test.cjs
node tests/subtitle-delivery-model.cjs
git diff --cached --check
git status --short
```

PowerShell 下 Node 通配符是否由运行时展开取决于版本，必要时用 Get-ChildItem 收集文件参数。可选代理测试需要先安装固定依赖。Android 构建/设备播放、真实播放器入口和原声听核属于独立验证；不能把隔离测试通过当作全量生产验收。

## 本轮核验记录

- 初次根目录 pytest 收集误扫本机第三方研究树，未进入执行；加入 testpaths 后消除该收集问题。
- 本项目完整 Python 运行共918项：916通过，2项旧文案/版本号断言失败，另7个 subtest 通过。随后只修正测试断言：网页版本不再写死 v=2；无效首集映射接受当前更早的范围门禁。对应 provider_isolation 和 remote_ui 两个文件共34项复测全部通过。未把这次专项复测写成另一次全量运行。
- Node 行为回归72项全部通过，字幕库 delivery model 独立脚本通过，static/*.js 全部语法检查通过。相同 Node 检查加入现有 GitHub Actions。
- Android `gradlew.bat --offline :app:testReleaseUnitTest` 成功；本次任务显示 UP-TO-DATE，核对现存4份报告共20项、0失败。未重新运行设备测试或发布 APK。
- 待提交文件检查无凭据/私钥命中（唯一带用户名密码的 URL 是拒绝非法 URL 的虚构测试输入），无 data/、logs/、媒体成品或环境目录。文档新入口的相对链接均存在。
- 保留现有 FastAPI TestClient 对 httpx 的弃用警告，本轮未为它升级依赖或变更生产服务。
