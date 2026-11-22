# 受控获取声明登记

`scripts/register_subtitle_acquisition_declaration.py` 给已登记的同集候选追加可核验的发布者软轨声明，供原 `studio.queue` 获取流程读取。默认只生成计划；显式 `--apply` 才在同一事务中更新该候选的 `source_metadata` 并追加不可变的登记历史。

```powershell
.\.venv\Scripts\python.exe scripts/register_subtitle_acquisition_declaration.py --request data/subtitle-studio/REQUEST.json --output data/subtitle-studio/PLAN.json
.\.venv\Scripts\python.exe scripts/register_subtitle_acquisition_declaration.py --request data/subtitle-studio/REQUEST.json --apply --output data/subtitle-studio/RECEIPT.json
```

请求使用 `subtitle-acquisition-declaration-request/v1`：明确 `candidate_id/show_id/season/episode/expected_input`，原 `source_metadata` 文本 SHA256、原候选 `title/url/detail/source_id/hash`、原来源 `id/kind/url`，以及私有报告的 `path/sha256` 和发布者页面实际链接的 `mediainfo_url`。输出路径必须是私有字幕目录下的新文件。

报告为 `subtitle-clean-acquisition-evidence/v1`，必须明确 `declaration_only=true`、`clean_frames_verified=false`，绑定同一候选和季集。报告保存真实 torrent 的 SHA256、infohash、完整物理文件清单、原发布页面、发布者链接的 MediaInfo HTML 和提取原文的 SHA256。入口重算 torrent infohash/清单；核 HTML 与提取原文一致；核 Matroska 文件名和实际被选中的唯一视频一致；逐条核独立 ASS、`S_TEXT/ASS` 和非强制轨。仅有文件名中的 `SRTx2` 不能登记。

登记前及短事务内重新核当前独立授权、字幕研究范围、未看且已播出、同一季集和 preparation input、来源启用和候选身份。已有负责人、停止事实、dispatch、租约、已交付、下载任务/未知回执、原取样会话和保真黑名单均保持保护。旧元数据字段完整保留；登记不修改候选标题、hash、状态、reason，或创建下载、取样、owner、input、制作任务。重复相同请求幂等。

`media_mode=soft` 始终带 `declaration_only=true` 和实际证据历史。它仅解除“缺少发布者洁净获取声明”的取料阻塞；实际完整文件到位后仍须核媒体身份、软轨关闭后的实帧、运行时和原绑定入口，中文候选仍走校正、跨版对轴和逐项覆盖，不能据此标记 ready/accepted/finished。

原 `Sources.remember` 只为数据库中有对应登记历史、且候选身份未变的声明保留元数据，避免空 RSS 元数据覆盖它。新出现的 hard/raw 或 hash 冲突保留历史并采用保守模式；冲突不会被下一次空 RSS 轮次清除。历史缺失或伪造的声明指针不会被保留。服务更新须沿原宿主加载或 supervisor，入口自身不启动服务或 Engine。
