# 单次派发调用方

## 2026-11-19 同负责人自动恢复

原协调任务对新合资格的停止轮次先核原生发送工具、原负责人、容量与预算，再给 `subtitle_begin_once.py` 传入 `--auto-recovery-turn 原精确轮次`。入口核对原票据和已记账的恢复动作，把原聊天 ID、固定输入、修订号、票据路径及简短续作要求一并交回。原生发送仅执行一次，协调任务立即保存真实宿主返回、使用原票据确认，再处理其它集。宿主丢失响应时保留中断事实。当前三张待对账的旧票据继续按原回执契约处理。

显式配置受信宿主桥的调用方也在确认前同步落盘正向答复中的线程与操作关联。归档绑定原票据摘要和修订号。核验仅有旧线程或零条检索结果时，仍缺少本次发送的返回事实。

## 2026-11-06：明确人工请求下的单集接续

`scripts/continue_subtitle_manual.py` 是独立的人工接续入口，默认只计划，实际写入须 `--apply`。它不是定时调度、重试、原票据补造或 not-created 对账入口。默认契约仅当用户对该集明确授权、原派发无负责人且租约已过期、原生完整终态证明精确 begin 在 `save_ticket → protect_ticket_directory` 的票据文件建立前失败，并且该原 turn 无 create/send/handoff 时才可使用。普通超时、票据遗失、已有负责人、已看、停止、有效租约、范围或当前输入不符均拒绝。

另有仅供明确人工请求的 `--preserve-timeout-intent` 契约，要求独立的 `subtitle-human-timeout-continuation/v1` 授权，用户须明确接受保留原未知结果、审阅原精确派发操作并授权当前聊天一次接续。绑定原完整持久终态/条目、精确 TimeoutExpired 命令、原 wrapper SHA；拒绝原 create/handoff，出现 send 也须逐字段证明是已审阅的工程消息，不是原制作派发。写入前用 psutil 实查本集、母片及原 wrapper 相关进程，访问失败即拒绝，并核观察不超过30秒；此检查只证明当前无相关 writer，不证明历史派发未发生。仍事务重核未看、无已知 owner、无停止、无有效租约、合法范围及固定 input。没有此独立授权和显式参数，原默认入口继续拒绝普通超时。

`--capability-evidence` 只接纳绑定同一固定输入、实际重新解码/字体/播放器核验的新能力证据，原过期报告及准备失败继续保留，不能仅延有效期。646154/S01E05 依用户“授权本集一次接续并完成”执行 r8→r9，原未知意图和凭据完整归档并保留，登记当前聊天唯一负责人；不是旧派发成功/失败补账。证据在 `data/subtitle-studio/646154-S01E05-complete-20261106/`。本授权不能用于其它集、普通自动调度或重复接续。

646154/S01E05 已沿原 finish/publish/实际播放器/业务 finished 路径完成 v0 r10→同 owner v1 r11 finished/completed，队列与字幕库实时回读均 done。两版均为中文观看版，日文精确时轴及具体未决项保留，不登记 accepted；新交付不改变历史 unknown。最终回执与原媒体/default/观看保护 SHA 核验见同目录 `RESULT.json`。

入口绑定 job/revision/input、用户原话、原生证据 SHA 及当前真实聊天 ID；事务重核范围、观看、唯一 owner 和原意图，原 `committing/unknown` 已占用的制作槽不新增。只接纳可真实重构的追加档案，不改原固定输入。原始整行及全部凭据只保存在私有、禁止 UPDATE/DELETE 的 `subtitle_delivery_manual_continuations` 历史表中，不导出或重建。当前行登记本次人工唯一负责人，并明确不宣称旧派发 created 或不存在；相同请求仅幂等读回。

486456/S02E05 已依据用户“授权本集一次人工接续，继续完成”执行一次，原 r13 和失败证据保留。后续仍走原 finish、publish、实际播放器检查和业务 finished 入口完成 v0→同 owner v1；人工接续本身不等于成品。证据位于 `data/subtitle-studio/486456-S02E05-complete-20261105/`。不将这一授权推广到其它未知派发或自动任务，也不重放该次接续。

## 2026-11-05：独立 begin 归档入口与超时取证

未部署宿主桥时，未来正常调度已确认实时 ready/容量/范围的工作可用：

```text
.venv/Scripts/python.exe -X utf8 scripts/subtitle_begin_once.py JOB --owner LABEL --revision N --expected-input INPUT --journal NEW_PRIVATE_DIRECTORY
```

目录必须全新，父目录已存在；失败目录、旧轮次和旧脚本保留，不复用或重放。该命令只执行一次原 `begin`，强制在领取租约前核 expected-input，退出 0 后再核原票据与本次输出，才写验证记录并输出原脱敏结果；不调用宿主、不 confirm、不自动恢复。`verified.json` 不能脱离成功退出和原票据验证单独当成功证据，更不证明 not-created。`committing/unknown` 旧意图不适用，不能用新目录绕过。

共用 runner 在启动前独占建立并 fsync 二进制 stdout/stderr、事件与 outcome 文件，记录 helper/直接子进程 PID、时刻、固定阶段；不使用 PIPE 等待后代关闭，不把异常中的部分 JSON 当结果。begin 等待预算120秒，其它调用30秒。超时只请求结束本次创建的直接子进程，最多再等5秒；记录 direct_reaped 和 descendants_unknown，**直接子进程退出不证明所有后代停止**。原有或其它制作进程不受该调用控制。超时文件是截至取证时刻捕获的数据，后代可能仍持有句柄，不宣称完整终态，也不授权释放、再次 begin 或宿主调用。已尝试宿主后的 confirm/interrupted 显式携带这一事实，不能因调用方自身不发宿主而误报为未发生。

CLI begin 向 stderr 写固定字段的阶段标记（真实 PID/PPID、时间和公开 job/revision），写日志失败不打断业务操作；没有标记只能说明证据缺失。返回原票据前不再同步导出全库静态视图，实时队列/字幕进度 API仍读当前事实，其它命令的导出保持。此处排除了一个可选视图阻塞回执的设计缺口；由于本轮646154/E5没找到原票据，**不能认定该导出是这次超时的根因**。

原 `data/subtitle-studio/delivery-runs/20261105-032416/begin_once.py` 未捕获 TimeoutExpired，partial 输出未归档，无法事后补成“已保存”。03:28:19的ticket-issued事件是摘要记录，不是持久票据；当前空搜索、进程快照或协调员未调用host的声明均不是可信操作级不存在证明。原646154/E5 r8及486456/E5 r13继续完整保护。本轮取证与限制见 `data/subtitle-studio/dispatch-timeout-repair-20261105/RESULT.json`。

## 已部署受信宿主桥后的完整调用方

`scripts/subtitle_dispatch_once.py JOB --owner LABEL --revision N --adapter-config PRIVATE_JSON --journal PRIVATE_EXISTING_DIRECTORY`

脚本先验证受信宿主桥接的create/resume能力和票据/审计目录，再begin、读取并核验原票据、只调用一次宿主、用原ticket确认。完整票据与CLI脱敏内联对象不能全等比较；仅比较schema/job/revision/owner/input/action/thread/issued_at等共享绑定字段，凭证只读原始受限文件。

宿主桥接为owner部署的绝对可执行命令argv JSON，stdin/输出按脚本模块说明；没有默认IPC、没有猜测内部协议、没有自动重试。失败或丢响应只记interrupted/诊断并保留committing，不据空投影推断not_created。created原ticket跨真实revision变动仍保留拒绝事实，交显式对账。

当前仓库没有可用于原生桌面create/resume的已部署桥接：模块及单命令调用方已隔离验收，生产迁移仍阻塞于此能力。不要用普通Codex CLI新会话代替项目原生owner，也不要为了接线修改任务目标或开强制票据开关。

桥接确实部署并隔离验收后，automation-2可将手写派发段替换为一句：

> 新派发仅调用经owner配置的subtitle_dispatch_once单次入口；其preflight拒绝时结束并记录缺失能力，不手写begin/票据比较/create/confirm，不重试未知结果。

在前置未满足前不能应用这句冒充完成迁移，旧手工核票据必须按共享绑定字段比较，不能重放历史派发。

`refresh`完整保留原committing/unknown行（含payload、revision、input和票据绑定），新观察通过实时readiness读取。原created事实先按原ticket登记，再独立核当前范围/观看/停止/owner是否允许应用；不把诊断当制作活动。以[派发回执契约](DISPATCH-RECEIPT-CONTRACT.md)的2026-11-04规则为准。

2026-11-05：begin在领取租约前用唯一无凭据探针实际执行独占写入、flush/fsync、关闭后读回和清理，提前读取模板/标题；真实ticket仍二次检查ACL并核完整读回及绑定。权限子进程预算45秒，begin调用总预算120秒，宿主桥接预算仍30秒；这不是自动重试。超时/非零退出/启动失败分别保留有限stderr和耗时，不打印命令或凭据。预检不能保证后续磁盘/权限不变，任何提交后失败继续保留committing，不能补造原ticket、清理租约或调用宿主。
