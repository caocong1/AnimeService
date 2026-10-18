# 派发"终态未创建"证据：宿主能力分析（研究侧提案，待 owner 审）

`dispatch-receipt/v1` 的 `not_created` 需要"该次派发不可能再完成，且确实没有创建负责聊天"的终态证据；在可信核验器出现之前，服务层默认记 `receipt_accepted=0, reason=evidence_invalid` 并保持 committing（见 [DISPATCH-RECEIPT-CONTRACT.md](DISPATCH-RECEIPT-CONTRACT.md)）。本文只分析宿主（本机 Codex 桌面应用）能提供什么、哪些事实可证明、证据应长什么样；不实现、不改生产。表结构核对于 2026-10-18 的本机投影库，只读了结构与计数，没有读消息正文。

## 宿主可观察面

Codex 把线程状态投影到 `~/.codex/` 下的 SQLite 文件；文件名带 schema 版本号（`state_5`、`thread_history_1`、`logs_2`），升级可能改表。

| 文件 | 表 | 与本题相关的列 |
|---|---|---|
| state_5.sqlite | `threads` | `id`、`created_at_ms`、`cwd`、`source`、`first_user_message`、`title`、`originator`、`archived` |
| state_5.sqlite | `thread_spawn_edges` | `parent_thread_id`、`child_thread_id`、`status`（见到 `open`/`closed`） |
| state_5.sqlite | `project_idempotency_keys` | 项目级幂等键；没有线程创建级别的幂等键 |
| thread_history_1.sqlite | `thread_turns` | `thread_id`、`turn_id`、`status`（见到 `completed`/`failed`/`inProgress`/`interrupted`）、`error_json`、`started_at`、`completed_at`、`rollout_end_ordinal` |
| thread_history_1.sqlite | `thread_items` | `thread_id`、`turn_id`、`item_id`、`item_type`、`item_json`、`started_at_ms`、`completed_at_ms` |
| thread_history_1.sqlite | `thread_history_projection_state` | `thread_id`、`next_rollout_ordinal`：投影消费到的位置 |
| logs_2.sqlite | `logs` | `ts`、`thread_id`：最近活动 |

调度线程创建/复用负责聊天时，宿主以 `mcpToolCall` 项记录调用：`tool ∈ {create_thread, send_message_to_thread, read_thread, list_threads, wait_threads}`，项内有 `id`、`status`、`error`、`durationMs`、`result.content`、`result.structuredContent`。没有看到"操作 id → 结果"的独立持久表；调用项的 `item_id` 是最接近宿主 operation id 的关联。

## 可证明与不可证明

**可证明"已创建"**（应记 `created`，不是 `not_created`）：

- 调度线程某 turn 内的 `create_thread`/`send_message_to_thread` 调用项 `status` 为完成、`error` 为空且 `result.structuredContent` 含新线程 id；或
- `threads` 中存在 `cwd` 为本项目、`created_at_ms` 不早于票据 `issued_at`、`first_user_message` 或 `title` 含本 job 标记（`begin` 生成的 session_prompt 内嵌 JOB_ID，建议再嵌入 dispatch 摘要前 12 位）的线程。

**可证明"终态未创建"**（以下全部成立）：

1. `host_ref` 指向的调度 turn `status ∈ {completed, failed, interrupted}`——turn 已结束，宿主不会再替它创建线程；
2. 投影已消费完该 turn：`thread_history_projection_state.next_rollout_ordinal > thread_turns.rollout_end_ordinal`；
3. 该 turn 内所有 `create_thread`/`send_message_to_thread` 项要么不存在，要么 `status` 失败或 `error` 非空，且 `result` 不含线程 id；
4. `threads` 中没有带本 job 标记、`created_at_ms ≥ issued_at` 的线程；
5. 没有 `parent_thread_id` 为调度线程、子线程创建时间不早于 `issued_at` 且子线程带标记的 `thread_spawn_edges`。

**不可证明（保持 unknown，走显式人工对账）**：turn 仍 `inProgress`（包括应用崩溃后永远停在 inProgress 的孤儿 turn）；投影落后于 rollout（条件 2 不成立，常见于应用未运行）；单次 `list_threads` 为空；任何"等了多久"的时间推断；证据 JSON 里自报的 `terminal: true`。

**投影终态不等于副作用终态。** `thread_turns.status` 与调用项 `status` 只说明 turn/item 在宿主记录里结束了；公开 app-server 文档的 turn/interrupt、turn/completed 也只定义 turn 与 item 的状态，没有承诺"某个外部创建请求以后绝不会完成"。因此上面五项同时成立得到的仍是 owner 的保守推断，不是宿主保证：在宿主提供 operation 级终态（可查询、持久、幂等的操作 id 与其最终状态）之前，`not_created` 只能作为"高置信缺席"由 owner 签核后使用，默认继续拒绝。调度 helper 里的 IPC `requestId` 只证明一次连接内的请求/响应关联，`timeoutMs` 或断连也不是终态取消证明；工具包装返回成功、turn 结束、列表未命中、日志静默都不是已验证的创建/缺席事实。

**create 与 reuse 必须分开。** 复用票据（resume）的失败永远不能证明原负责聊天消失，不得清空 owner；只有 `dispatch_action = create` 的派发才谈"未创建"。

## 证据契约建议

证据是调用方的声明，核验器是权威。分两步：先由只读快照步骤（独立进程，mode=ro、query_only、短事务、按索引范围查询，只取 type/status/调用名/关联字段，不 SELECT 消息或参数正文）生成**不可变快照文件**并记录其 SHA-256；`absence_verifier(ticket, evidence, host_ref)` 在回执事务内只做快速核验——快照文件存在、hash 与证据一致、快照由可信本机身份生成、快照内各字段满足全部条件、`issued_at`/`checked_at` 绑定成立——不在写事务里查活库、等待网络或聊天。任一条件不满足或异常即返回非 True。

```json
{
  "method": "codex-projection-readonly/v1",
  "checked_at": "2026-10-18T15:20:00+08:00",
  "checked_by": "codex_absence_verifier --dry-run",
  "job_id": "episode:…", "dispatch_digest": "…",
  "dispatcher_thread_id": "…", "turn_id": "…",
  "turn_status": "completed", "turn_completed_at": 0,
  "projection": {"next_rollout_ordinal": 0, "turn_rollout_end_ordinal": 0},
  "call_items": [{"item_id": "…", "tool": "send_message_to_thread", "status": "failed", "has_thread_result": false}],
  "marker_threads": 0, "spawn_edges_after_issued": 0,
  "projection_files": {"state_5.sqlite": {"size": 0, "mtime_ms": 0}, "thread_history_1.sqlite": {"size": 0, "mtime_ms": 0}},
  "schema_versions": {"state": 0, "thread_history": 0}
}
```

- `host_ref` 建议格式 `thread:<dispatcher>/turn:<turn>[/item:<item>]`，由调度 automation 在 `begin` 之后、调用宿主之前记录；没有 `host_ref` 就无法定位 turn，核验器直接返回非 True。
- 绑定：`job_id`、`dispatch_digest` 与票据一致；`issued_at` 作为所有时间比较的下界；`checked_at` 带时区、不早于 `issued_at`、不晚于现在 5 分钟。
- 投影库持续变化，不能用内容哈希绑定；记录三个库的 size/mtime 与 `_sqlx_migrations` 最高 version，核验器发现 schema 版本不在已知集合时 fail closed。
- 读活库用 `mode=ro` 加 busy_timeout（WAL 下读安全），不要 `immutable=1`。
- 报告与快照只保留字段名、状态类别、脱敏关联 id 与证据缺口；不放消息正文、完整 token 或带凭据的 URL。
- 关联链必须原始且完整：job/input/dispatch 摘要 → 原始 tool call 项 → 宿主 operation/request（若存在）→ 实际 thread id 或终态证据；任一环缺失即报告"能力不足"，不用 clientThreadId 或包装返回补链。

## 边界与风险

- 投影是 Codex 内部实现，不是公开 API；本文的表名/列名是 2026-10-18 的事实，升级后必须重新核对。
- 条件 1 依赖调度线程知道自己的 turn id。若 automation 无法得到，退化为 `thread:<dispatcher>` 加 `issued_at` 之后的 turn 集合：集合内任一 turn 仍 inProgress 即不终态。
- 标记检索依赖 session_prompt 内嵌的 JOB_ID；手工创建、改了提示词或标题的线程检索不到，只能靠条件 3 的调用项结果。
- 核验器本身是守卫同类的本机只读脚本，应沿用 `scripts/codex_dispatch.json` 的 `codex_home`/`db_files` 配置与 fail-closed 约定，不新增写权限。

## 结论

现有投影能可靠证明"已创建"，不能证明"不可能再创建"。`not_created` 的自动接受应等待宿主提供 operation 级终态查询；在此之前可实现的只是只读快照 + 人工签核的保守流程，默认仍拒绝。

## 建议落地顺序

1. owner 确认调度 automation 能否在调用宿主前记录 turn/item 级 `host_ref`；不能则采用退化方案。
2. 研究侧写 `scripts/codex_absence_verifier.py`（只读、读 codex_dispatch.json、fail closed）与合成投影库的隔离测试；生产先只跑 `--dry-run` 产出证据供人工对照。
3. owner 审查后把核验器作为 `SubtitleDelivery(absence_verifier=…)` 注入 CLI；`dispatch_receipts_required` 仍保持关闭，直到调用方迁移完成。
