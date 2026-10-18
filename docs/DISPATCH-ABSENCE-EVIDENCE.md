# 派发“终态未创建”证据：宿主能力与只读采集边界

Owner 第13轮审定（2026-10-18）。保留研究侧结构、状态集合与键名分析；以下区分观察事实、诊断推断与尚未获得的宿主保证。本轮仅落地文档，不实现采集器、不改生产或提示词。

`dispatch-receipt/v1` 的 `not_created` 要求该次派发不可能再完成且没有创建负责聊天。当前生产未配置可信终态核验器，默认 `receipt_accepted=0, reason=evidence_invalid`，保持 committing。五项筛查全通过、人工签核或本机 SHA-256 均不改变此边界；见 [DISPATCH-RECEIPT-CONTRACT.md](DISPATCH-RECEIPT-CONTRACT.md)。

## 宿主可观察面

研究侧于2026-10-18只读核对本机 `~/.codex/` 投影结构、计数、状态集合与键名，未读消息正文。文件名与字段属于内部实现，不构成跨版本 API。

| 文件 | 表 | 与本题相关的列 |
|---|---|---|
| state_5.sqlite | `threads` | `id`、`created_at_ms`、`cwd`、`source`、`first_user_message`、`title`、`originator`、`archived` |
| state_5.sqlite | `thread_spawn_edges` | `parent_thread_id`、`child_thread_id`、`status`（见到 open/closed） |
| state_5.sqlite | `project_idempotency_keys` | 观察到项目级幂等键；未在已检查投影中发现线程创建级幂等保证 |
| thread_history_1.sqlite | `thread_turns` | `thread_id`、`turn_id`、`status`（见到 completed/failed/inProgress/interrupted）、`error_json`、`started_at`、`completed_at`、`rollout_end_ordinal` |
| thread_history_1.sqlite | `thread_items` | `thread_id`、`turn_id`、`item_id`、`item_type`、`item_json`、`started_at_ms`、`completed_at_ms` |
| thread_history_1.sqlite | `thread_history_projection_state` | `thread_id`、`next_rollout_ordinal`：投影消费位置 |
| logs_2.sqlite | `logs` | `ts`、`thread_id`：活动线索；静默不证明副作用结束 |

研究侧识别出 mcpToolCall 项内 tool/status/error/result 等键及 create_thread、send_message_to_thread 等调用名。已检查投影未见“持久 operation id → 最终副作用结果”独立表；不能据此断言整个宿主不存在该能力。item_id 仅定位调用项。私有 IPC requestId 只关联请求响应，超时、断连、中断均不是副作用已取消的证明。

## host_ref 的取得与退化边界

[公开 app-server 文档](https://learn.chatgpt.com/docs/app-server)描述宿主可观察的 turn/item 生命周期与标识。宿主控制器能观察，不等于当前 automation 模型在发起工具前已获知自己的精确标识；目前未验证有此透传能力。公开协议也没有给出本文所需的外部创建操作终态保证。

1. 若未来可信宿主桥接实际提供当前 thread_id/turn_id，可在调用前把 `thread:<dispatcher>/turn:<turn>` 连同原票据摘要归档为派发意图。调用开始后观察到真实 item，再追加 `/item:<item>` 关联，不能预造未来 item id。
2. 补充关联须保留来源、采集时刻，不改原始 begin 票据，不更换已存回执引用绕过幂等。CLI 的 --host-ref 已可传递真实引用；现在不改提示词或强制开关。
3. 不把守卫“最新 turn”、clientThreadId、连接 requestId 或自造 UUID 冒充原调用身份。并发、steering、投影延迟都可能使“最新”与原调用不同。
4. 缺精确 turn 时，thread + 时间区间只用于诊断候选，显式报告 host_ref_unbound。仅查 started_at >= issued_at 会漏掉 begin 之前已启动的当前 turn；须纳入区间重叠、未结束与缺失数据。有限候选全部结束也不能证明没有其他仍可完成的调用。

因此接受退化方案用于排查，不接受它作为 not_created 核验依据。缺精确关联也不能把候选线程直接认领为该次派发的创建结果。

## 创建事实与缺席筛查

**创建/复用事实**需要原始 job/input/dispatch 票据摘要、真实调用项与可信宿主结果中的负责 thread id 形成可核对关联。完整结果可作为 created 事实候选，仍须经过服务层票据、revision、owner、范围及线程冲突门禁。send_message_to_thread 可以复用已有线程，不能一概称为“新线程已创建”。

JOB_ID、cwd、标题、创建时间和 spawn edge 仅辅助定位。标记可复制/改写，多次派发可共用 JOB_ID；仅标记命中不自动确认 created 或转移 owner。本文不授权修改 session_prompt。未实施正文/标题检索必须报告 marker_check_not_performed，不能报0命中。

以下五项称为**投影缺席筛查**，即使全部成立也没有宿主 operation 级不可再完成保证：

1. 精确关联的 turn 在投影中为 completed/failed/interrupted；这只描述投影状态结束。
2. 已知 schema 下可核对该 turn 的消费水位；空值、未知 ordinal 语义或 schema 漂移均为缺口。水位不承诺宿主不会追加事实。
3. 相关创建/复用调用项未见成功线程结果；无调用项、失败、空结果与完整负面事实必须分别报告。
4. 已授权且完成的标记核对未见候选线程；未执行、截断或失败不能视为空集合。
5. 已检查相关 spawn edge 未见候选子线程；open/closed 不保证外部请求已取消，表中也没有本分析确认可直接使用的 edge 创建时间列。

inProgress、孤儿 turn、投影滞后、列表为空、超时、日志静默、调用方自报 terminal:true 均不能证明终态未创建。reuse 失败不能证明原负责聊天消失，不清 owner。人工对照只归档观察与缺口，不绕过默认拒绝、释放租约或重投。

## 诊断采集器范围（批准研究侧独立分支实现）

可用 scripts/codex_absence_verifier.py 文件名，但职责仅为**只读诊断快照采集器**。不提供可注入服务层并返回 True 的 absence_verifier，不调用 IPC/业务写接口或修改任务配置。

- 读取 scripts/codex_dispatch.json 的 codex_home/db_files/cwd/targets，支持既有环境覆盖；不导入有配置写入/调度执行副作用的入口。路径、线程范围、原票据与 schema allowlist 显式校验。未知 schema、缺表列、坏类型、权限/读取失败均输出 unsupported/incomplete 与非零退出。
- 生产仅允许显式 --dry-run，指定原票据、可用真实 host_ref、受限输出路径。缺绑定可出诊断但标明不完整。无默认生产全库扫描、无 rollout 扫描或后台循环。限制查询范围、行数、文件大小、总时限；截断显式 incomplete。退出0只表示明确声明的诊断范围执行完整，绝不表示允许 not_created。
- 活库 mode=ro/query_only、短只读事务、有界 busy_timeout。不用 immutable=1 绕过 WAL，不 checkpoint、不迁移、不复制不一致的 sqlite 主文件冒充备份。WAL 可能依赖已有侧文件和权限；只读连接不等于零文件系统触碰，不自动补权限或修复环境。
- 每库是独立快照，多库没有共同原子时点。记录各库采集起止、schema 版本/迁移集合、水位、覆盖范围；矛盾/变化/缺口保留。size/mtime 仅元数据，不能证明一致性、新鲜度或可信来源；未查询的库标明 unused，不伪造已检查。
- 持久化规范化、脱敏、不可变报告并记录实际文件 SHA-256。hash 绑定报告完整性，不绑定持续变化的活库，也不证明来源可信或“不可能再创建”。报告始终 diagnostic_only=true、absence_authorized=false，不输出 terminal=true。
- 不 SELECT/输出消息正文、标题、first_user_message、工具参数、result.content 或完整 item_json。必要时只在限定调用项上于 SQL 内投影已知 JSON 键的类型、tool/status、线程关联等最小白名单字段，不把正文容器送入进程/日志。供审报告关联 id 脱敏；票据只输出摘要，不输出 token 或带凭据 URL。未经单独审查不做正文标记检索。
- 票据 issued_at 当前为 Unix 秒，created_at_ms/started_at_ms 为毫秒。按已验证 schema 显式标注/转换，缺失、非有限、异常范围值 fail closed，不按数值大小猜单位。checked_at 带时区且满足票据时间边界；turn 可早于 begin 启动，不能混淆 turn 启动与调用发生时间。

最小报告形状如下，字段值仅为结构示意，不能提交为终态证据：

```json
{
  "schema": "codex-projection-diagnostic/v1",
  "method": "codex-projection-readonly/v1",
  "diagnostic_only": true,
  "absence_authorized": false,
  "job_id": "redacted",
  "ticket_digest": "sha256",
  "dispatch_digest": "sha256",
  "host_ref_binding": "unverified",
  "observations": [],
  "coverage": {"truncated": false, "cross_database_atomic": false},
  "gaps": ["host_operation_terminal_api_unavailable", "marker_check_not_performed"],
  "projection_files": [],
  "schema_versions": {},
  "collected_from": "timezone-aware timestamp",
  "collected_until": "timezone-aware timestamp"
}
```

缺席、关联未知、未实施的检查、unsupported 必须区分；发现绑定充分的 created 候选也仅报告，不自动写业务状态。

## 隔离验收与接入条件

研究侧以本文落地后的 main 为基线独立提交。合成投影测试至少覆盖：精确/缺失/伪造 host_ref；begin 前已启动 turn；秒/毫秒；成功线程结果与 reuse；空结果、孤儿 turn、投影滞后、跨库矛盾；未知 schema、缺表、坏 JSON、权限失败；截断与总时限；WAL 已提交数据可见；原库内容不写、正文/参数/token 不泄露。五项全通过仍断言 absence_authorized=false。测试不得连接真实宿主或生产。

Owner 审查后才在生产取短 --dry-run 快照供人工对照。当前不注入 SubtitleDelivery、不新增人工签核绕过路径；dispatch_receipts_required 继续关闭。未来只有取得可绑定原派发、持久可查询的宿主 operation 级“已终结且无副作用”契约后，才另审授权核验器与调用方迁移。

未来核验器即使获批，回执写事务内也只能快速核对事先取得的不可变可信证明及 hash/身份/绑定/有效期，不查活投影、不等 IPC。普通调用方 JSON 的 hash 与 checked_by 不是可信证明；可信来源、抗替换、重放边界须另审。revision 变化后的 created 恢复沿现有只读 reconcile-plan 与未来显式 CAS 契约，不借缺席采集放松门禁。
