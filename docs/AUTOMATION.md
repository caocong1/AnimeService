# 定时任务与持续制作

2026-10-16 日文来源更新：automation-2准备/交接与automation-4新材料复盘执行 [逐集日文来源](SUBTITLE-JAPANESE-SOURCES.md)。逐集核Jimaku/用户指定页的实际下载，区分查无/查失败/候选/待匹配/已匹配，保留来源声明及SHA，明确日文对白覆盖。学生会E2已有4份候选；新材料由原调度沿唯一owner增量处理，不把材料核实写成成品交付。两个任务通过应用工具更新提示词并回读；周期、目标聊天和原资源/已看/owner保护保持，回执见私有`data/subtitle-studio/jimaku-12682-verification-20261016/`。

配置核对日期：2026-10-15；时间按 Asia/Shanghai。这里只保存本项目的方式和脱敏模板，不创建、更新或恢复任何任务。任务是否仍 ACTIVE、当前负责人和输入版本，执行前须重新读取应用配置与实时队列。

## 当前分工

| 入口 | 核对时状态与周期 | 职责 |
| --- | --- | --- |
| automation-2：字幕v0→v1连续制作与派发 | ACTIVE，每15分钟，原聊天 heartbeat | 在追材料准备、去重派发、跟踪唯一单集负责人；不逐句写正文 |
| automation-3：想看首集实评与稳定更新 | ACTIVE，每小时，原聊天 heartbeat | wish/trial 首集实评、主备选源及后续稳定更新；样本由后台提交 |
| automation-4：字幕遗留问题每周复盘 | ACTIVE，每周日10:00，原研究聊天 heartbeat | 只对具体未决项的新材料/新技术条件验证；已看集不自动改字幕 |
| AnimeService-CodexDispatchGuard | 独立 Windows 本机任务，每5分钟 | 只检查指定两个调度 heartbeat 的无进展轮次；不承担制作或重投 |

其它项目的定时任务不属于这里。旧“字幕每小时改几句”、旧迅雷 UI heartbeat、每两小时原声循环已被后续决定覆盖；不要照历史配置复建。

## 一轮调度如何工作

1. 读 [当前规则](PROJECT-RULES.md) 和必要专题，再读实时队列/短交接，只看新增证据。
2. 复核 selected、watching/wish/trial、授权、映射、盘点、逐集边界、cleanup_hold、watches.finished 及原 owner/lease。
3. 准备或跟踪本轮可执行动作。就绪后先 `begin` 持久意图，再按返回 action 创建单集聊天或恢复指定原聊天；记录真实 threadId，成功后 `confirm`。
4. 外部结果未知先 `unknown` 并核对实际会话；不能因时限或守卫中断重新创建。active 不重复发继续，跟踪使用 cursor 与有界 wait。
5. 更新私有短交接、实际阶段和下一动作，然后结束。通常3–10分钟内完成有界检查；等待素材、owner 或下个时点不常驻同轮。

单集负责人持续完成 v0→v1，不等下一次 heartbeat 才改下一批。v0 finished 回执若返回 continue_refinement=true，保留唯一 writer 并立即继续；v1 六项、实际成片和播放器回执核过后才结束。已看保护与停止 owner 例外均由宿主当前事实决定。

本机控制入口为 `scripts/subtitle_delivery_queue.py`。`status` 只读；`refresh`、`begin`、`confirm`、`progress`、`finished`、`blocked`、`refine` 等会修改调度状态，不是无害演示。先用 `--help` 查看当前参数；不要复制文档中的旧 job/revision 到生产执行。

## 安静、去重和失败退出

无变化、等待、旧失败和同文晚稿保持安静。仅真实交付版本、新实质失败或用户操作需求通知，并持久去重。材料已到、任务已创建、模型已安装都不等于成品。准备/编码/等待/返工分别计时，不能拿自然日跨度当实际工作时长。

疑难先限量验证；没有新证据就记录已尝试条件、当前处理与下一可证伪条件。周复盘查新方案，不能承担 v1 的日常画面字、OP/ED、位置或动效工作。付费或私有音视频外传先说明实际范围和成本，由用户决定。

## 卡住识别与恢复

2026-10-15 两个调度曾在工具完成后停留模型 HTTP 等待；真实恢复经过应用精确 expectedTurnId 中断和 completed 手动压缩。不能以“思考中”标签推断一直有工作，也不能以 rollout 总大小等同当轮请求大小。

`.codex/config.toml` 入库的两个设置为自动压缩阈值200,000 token、工具输出上限6,000 token；不设置模型、账号或网络。驻留旧线程未必立即载入新项目配置。不要为此自行重启应用。

守卫源码为 `scripts/codex_dispatch_guard.py` 和 `scripts/codex_dispatch_control.cjs`。只有指定调度会话、项目路径、匹配 heartbeat（或精确登记的恢复轮次）、inProgress、至少20分钟无日志进展、无运行工具同时成立才经应用 IPC 按 expectedTurnId 中断。它只读 Codex 状态，不改 Anime DB、不释放业务租约、不创建 AI 轮次、不杀应用、不处理中途工作的单集 owner。

不带 `--apply` 只判断并写私有诊断快照，带 `--apply` 才可能中断。调度会话白名单、恢复轮次、项目路径、`node` 路径、Codex 数据库文件名与状态目录都在 `scripts/codex_dispatch.json`，两个脚本共用；`ANIMESERVICE_DISPATCH_CONFIG` 可指向另一份。`wait_seconds` 单位为秒，范围 60–86400，非整数或越界回退旧默认 1200；`targets` 为空、`cwd` 为空或 `db_files` 缺项则拒绝启动。`node` 先取 PATH 再取配置路径。每次运行都会写 `status.json`，读取 Codex 状态失败时 `error` 字段说明原因，控制步骤的结果与错误按线程记在 `actions.jsonl`。克隆不会注册 Windows 任务，新环境不能直接认为可运行；迁移应先改配置文件中的目标和依赖，再隔离测试和人工核对。

`scripts/codex_absence_verifier.py --dry-run --ticket TICKET.json --host-ref thread:<id>/turn:<id> --out REPORT.json` 只读采集同一组投影库里与一次派发相关的 turn 状态、投影水位、create/reuse 调用项（SQL 内投影白名单字段，不取正文）与 spawn edge，写出不可变、脱敏的诊断报告并附 sha256；它不是核验器，报告始终 `absence_authorized=false`，退出 0 只表示声明的诊断范围跑完，非 0 表示不支持的 schema、库不可读、截断或超时。边界见 [DISPATCH-ABSENCE-EVIDENCE.md](DISPATCH-ABSENCE-EVIDENCE.md)。详见 [调度健康](CODEX-DISPATCH-HEALTH.md)。

## 模板与迁移

本次从本机三个任务的 prompt 字段生成 [提示词模板目录](automation-prompts/README.md)，不复制账号、真实目标 threadId 或其它项目配置。manifest 记录原提示词 SHA-256，原自动化文件仍在本机。模板中的线程占位符必须重新绑定；先读当前规则，不能粘贴模板就继承旧用户授权。

用户请求迁移/修改时，通过应用的任务工具读取后更新，保留无关字段并回读结果；本文不建议直接写运行中的 automation.toml。项目采用的准备、唯一派发、完整制作和守卫限制来自本机实现，不是通用产品保证。[OpenAI 官方定时任务说明](https://developers.openai.com/training/walkthroughs/scheduled-tasks)可供了解周期入口和本机运行条件；本项目具体规则以这里及实时台账为准。
