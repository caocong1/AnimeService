# 单次派发调用方

`scripts/subtitle_dispatch_once.py JOB --owner LABEL --revision N --adapter-config PRIVATE_JSON --journal PRIVATE_EXISTING_DIRECTORY`

脚本先验证受信宿主桥接的create/resume能力和票据/审计目录，再begin、读取并核验原票据、只调用一次宿主、用原ticket确认。完整票据与CLI脱敏内联对象不能全等比较；仅比较schema/job/revision/owner/input/action/thread/issued_at等共享绑定字段，凭证只读原始受限文件。

宿主桥接为owner部署的绝对可执行命令argv JSON，stdin/输出按脚本模块说明；没有默认IPC、没有猜测内部协议、没有自动重试。失败或丢响应只记interrupted/诊断并保留committing，不据空投影推断not_created。created原ticket跨真实revision变动仍保留拒绝事实，交显式对账。

当前仓库没有可用于原生桌面create/resume的已部署桥接：模块及单命令调用方已隔离验收，生产迁移仍阻塞于此能力。不要用普通Codex CLI新会话代替项目原生owner，也不要为了接线修改任务目标或开强制票据开关。

桥接确实部署并隔离验收后，automation-2可将手写派发段替换为一句：

> 新派发仅调用经owner配置的subtitle_dispatch_once单次入口；其preflight拒绝时结束并记录缺失能力，不手写begin/票据比较/create/confirm，不重试未知结果。

在前置未满足前不能应用这句冒充完成迁移，旧手工核票据必须按共享绑定字段比较，不能重放历史派发。

`refresh`对同input、同scope/未看事实的活动工作只更新诊断payload并记readiness-observed，不改变派发revision或活动updated；真实input/scope/已看变化仍推进revision并使旧票据失效。队列不把诊断事件当作owner制作活动。
