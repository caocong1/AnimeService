# 时轴对齐提案的接入说明：从结果 JSON 到独立核心的 alignments

`python -m anime.subtitle_alignment` 只产出 `status=proposed` 的分段时轴提案。它不碰独立核心、工作簿、DB 或 owner；
是否采用、何时采用，由原机唯一负责人按未看集、不可变版本规则决定。本文不含任何生产路径或作品信息。

## 1. 完整结果 JSON 与 proposal 映射字典是两样东西

`--out` 写出的完整结果 JSON（`schema = anime-subtitle-alignment-proposal/v3`，`revision = interval-overlap-dp-v3`）的顶层键：

| 键 | 含义 | 用途 |
|---|---|---|
| `proposal` | `{draft_id: {...}}`，独立核心 `alignments` 条目的形状 | **唯一**可交给 `python -m subtitle_lab workbook --alignments` 的部分，且必须先经真实审核改写 |
| `summary` | 分段数、剪辑数与 `cut_position_kinds`、配对计数、覆盖率及其定义 `target_time_covered_basis` | 判断材料是否值得复核 |
| `segments` | 每段源/目标跨度、`offset_ms`、单元数、锚定数、`hit_z`、`confidence` | 段**偏移**是否可信 |
| `cuts` | 相邻段之间的剪辑：`candidate_time_ms`（代表点）、`position`（证据区间）、`segment_confidence` | 剪辑**位置**有多精确，与段偏移分开看 |
| `review_points` | 复核清单：`cut`、`drift`、`segment_confidence`、`target_missing_run`、`candidate_extra_run`、`splits`、`outside_target` | 负责人逐条实看实听 |
| `pairs`、`unreferenced_candidate`、`control_rows`、`outside_target`、`target_without_candidate` | 行级时间配对与未配对行；`control_rows` 是音符/括号音效行，只记录时间重叠 | 定位缺口、歌词、音效 |
| `lyrics`、`text_comparison` | 歌词块覆盖统计；同语种草稿的可选文本比对 | 都不是声学验收 |
| `inputs` | 两个输入文件的文件名、大小、sha256、角色统计 | **字幕文件 hash，不是母片 fingerprint** |
| `caveats` | 固定告警 | 随结果一起交接 |

proposal 条目：

```json
{"<draft_id>": {"target_fingerprint": null, "status": "proposed", "reviewer": null, "evidence": null,
                "tool": "anime-subtitle-alignment-proposal/v3 interval-overlap-dp-v3",
                "segments": [{"source_start_ms": 0, "source_end_ms": 184484,
                              "target_start_ms": 410, "target_end_ms": 184894}]}}
```

独立核心 `Archive.mapped_cue` 的规则：草稿 metadata 里的 `media_fingerprint` 等于当前母片 fingerprint 且时轴不是估计值时不需要映射；
否则只有当条目的 `target_fingerprint` 等于当前母片 fingerprint、`status == "accepted"`、`reviewer` 与 `evidence` 非空时才使用 `segments`。
四个毫秒值必须是 int，`source_end_ms > source_start_ms`，`target_end_ms > target_start_ms`，全部 ≥ 0；
任何 cue 不完整落在某一段内都会抛错（cue crosses edit）。工具的 `segments` 已满足这些约束，人工改动后仍须满足。

## 2. 最小提取示例

```bash
python -m anime.subtitle_alignment --target <母片中文基稿.ass> --candidate <日文草稿.srt> \
    --out <私有目录>/alignment-result.json --draft-id <独立核心中的草稿 id> [--dialogue-styles Text_CN,Top_CN]
python - <<'EOF'
import json
result = json.load(open('<私有目录>/alignment-result.json', encoding='utf-8'))
with open('<私有目录>/alignments.proposed.json', 'w', encoding='utf-8') as f:
    json.dump(result['proposal'], f, ensure_ascii=False, indent=1)
EOF
```

`--out` 不能指向输入文件或已存在的文件；`--overwrite` 只能替换自己的旧输出。默认输出不含字幕正文，`--include-text` 只用于私有目录。

## 3. 最小检查示例

```python
import json
from anime import subtitle_alignment as sa

result = json.load(open('alignment-result.json', encoding='utf-8'))
draft_id, entry = next(iter(result['proposal'].items()))
assert entry['status'] == 'proposed'                       # 工具永远不写 accepted
rows = sa.load('<日文草稿.srt>', False)                     # 与工具相同的解析与编号
unplaced = [r['id'] for r in rows if r['valid'] and sa.mapped(entry['segments'], r['start_ms'], r['end_ms']) is None]
assert unplaced == [o['id'] for o in result['outside_target']]   # 这些行进入核心会抛错，必须删除或归段
for point in result['review_points']:
    if point['kind'] == 'cut':
        print(point['candidate_range_ms'], point['position_kind'], point['target_review_range_ms'],
              point['target_review_rows'], point['reason'])
```

`tests/test_subtitle_alignment.py::test_proposal_extraction_and_check_flow` 用合成数据执行同样的步骤。

## 4. 剪辑位置：代表点与证据区间

- `cuts[i].candidate_time_ms` 只是代表点（所选 cue 空隙的中点），`candidate_time_basis` 写明这一点。
- `cuts[i].position.candidate_range_ms = [低, 高]` 是按两侧偏移下的 cue 证据，剪辑可以落在的候选时间范围。端点在 cue 簇边界上，重叠 cue 不会被切开。
  - `kind = single_gap`：范围就是一个 cue 空隙（可能很长，例如整个 OP）。
  - `kind = undecided_cues`：范围内还有在两种偏移下都说得通的 cue 簇，id 列在 `undecided_candidate_ids`。
  - `supporting_candidate_ids.before/after` 是阻止范围继续扩大的 cue 簇；`bounded_by_evidence` 某侧为 false 表示那一侧已到段的尽头。
  - `cue_free_ms`、`longest_cue_free_gap_ms` 说明范围内没有 cue 的时长。
  - `evidence_tolerance`（0.5）是移动剪辑可放弃的去随机化支持分，约等于一条完全匹配的 cue 簇。**这是证据上界，不是统计校准的置信区间。**
- `target_review_range_ms`、`target_review_rows` 是母片侧对应的复核范围（两侧偏移下的最小与最大映射）。
- `segment_confidence` 是两侧段偏移的可信度（来自 `hit_z` 与锚定单元数）。段偏移可信不等于剪辑位置精确，两者分别记录、分别复核。
- 负责人若在范围内把剪辑定到别处，要相应改写 proposal 中前一段的 `source_end_ms/target_end_ms` 与后一段的 `source_start_ms/target_start_ms`，并保持第 1 节的约束。

## 5. 交接：从 proposed 到 accepted 必须真实发生的事

1. 逐条处理 `review_points`：`cut` 实看实听整个 `candidate_range_ms` 与 `target_review_range_ms`；`drift` 核首尾锚点；
   `segment_confidence` 非 high 的段抽样审听；`target_missing_run`、`candidate_extra_run` 判断是缺行、歌词、字卡还是剪辑；
   `splits` 为 one_to_many 行写可审计的拆分计划；`outside_target` 行删除或归段。
2. 不确定区间（`position_kind = undecided_cues`，或 `single_gap` 但 `cue_free_ms` 很长）必须进入负责人的复核清单，不能用代表点代替。
3. 填写 `target_fingerprint`：独立核心对母片文件计算的 fingerprint（`subtitle_lab.core.fingerprint(media)`，与工作簿的 `target.fingerprint` 相同）。
   **不是** `inputs.target.sha256`，那是字幕文件的 hash。
4. 填写 `reviewer`（真实审核者）与 `evidence`（逐段核验记录：看了哪些时间点、听到什么、剪辑最终定在哪里、依据何在）。
   核心只检查非空，但空壳 evidence 等于造假；只改 `status` 不写证据不算审核。
5. 以上完成后才把 `status` 改为 `accepted`，另存 `alignments.accepted.json`（不覆盖 proposed 文件），由原机唯一负责人执行
   `python -m subtitle_lab workbook --show … --season … --episode … --base <中文基稿 id> --media <母片> --alignments alignments.accepted.json --refresh`。
   只对未看集、沿现有唯一 owner、生成新的不可变版本；研究电脑不运行它。
6. 时间配对覆盖、人工锚点一致、文本相似都不是声学验收；`finished`、`edition`、`accepted` 继续分别记录。

## 6. 禁止

- 把字幕文件 hash 当 fingerprint；只改 status；用均匀平移代替分段；跨剪辑拆 cue。
- 在研究电脑或非 owner 会话运行 workbook refresh、发布、DB 写入或唤醒任务。
- 用工具输出覆盖输入或旧成品：工具本身拒绝，人工复制时同样适用。
