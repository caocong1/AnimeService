# 单文件字幕参考候选的受控登记

`scripts/register_subtitle_reference_candidate.py --request 私有请求.json` 默认只校验。显式 `--apply` 只追加不可变证据、同 subject 且已核的 Mikan RSS source、一个 `reference_only` candidate 和 `subtitle_reference_intakes` 回执，不发下载、不改原 owner/input/母片/观看/default/accepted，也不改变全局季集映射。

此入口仅服务已有 `blocked / blocked-stopped / repair_blocked` 原负责人新增真实参考的情形。请求绑定精确 owner、input、固定 CR 母片、当前映射 SHA、真实单文件 torrent/hash/manifest；事务中重核独立授权、未看、已播、季集范围、无有效租约和未知回执、黑名单、保真、参考数及空间。旧租约 token 和恢复台账保留。证据必须在私有档案内且 SHA 一致；Mikan work/release/RSS、Bangumi ep/sort、ABEMA 官方标题、发布者链接的 NFO 和已有 Jimaku 原稿来源须相互一致，当前网页取证有效期六小时。

候选专属映射只增加这一个完整物理文件名别名，以及一个累计编号到本地集号的规则；标题与文件仍须明确含原季号，`require_season` 不放宽。映射绑定原 candidate 的完整身份和 expected hash。RSS 刷新保留限制；身份或声明冲突会持久封锁，不能用后续刷新消除。新建的专属 RSS source 不能顺带登记其他集候选，复用原已核 source 时不改变原 source 行为。

登记后由协调者先沿现有 supervisor 加载代码，再且仅再经现有后台 `POST /api/subtitle-studio/{show_id}/{episode}/acquire`，参数为 `{"candidate_ids":[登记所得ID],"role":"subtitle_reference"}`。该后台在取 torrent 前、真实 manifest 后、写原任务前均复核证明与原负责人保护；仍使用原 Engine 和下载器。不要用 `/api/shows/{show_id}/acquire-material`：普通下载、首集选源、clean_base、自动 discover 及母片绑定均拒绝此候选，持久回执在 metadata 标记遗失时也继续限制普通入口。

HTTP 返回不明时停止重投。先按登记的精确 infohash 只读核 `tasks`、`subtitle_samples.evidence.workflow_role`、原 `thunder_queue`/dialog hold 和下载器实际回执，交原后台进行已有任务对账；不删 intent/uncertain、租约或旧回执，也不自行重新 enqueue。只有 `acknowledged=1`、真实下载状态和精确 selection 同时成立才能报告已取料。

NFO 声明有日文 ASS 且条数与已存原稿相同，只是平台/来源线索。登记与下载均不证明全文同文、同钟、原声已核、对轴完成或准备就绪。素材完成后须实提轨道、比较文本时轴，并以多锚音频/场景证据验证参考到固定母片的映射，再由原 owner 受控恢复；不得把全局偏移提案改成 verified。

隔离验证：`.venv\Scripts\python.exe -m pytest tests/test_subtitle_reference_intake.py -q`。
