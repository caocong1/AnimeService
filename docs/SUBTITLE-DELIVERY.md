> 2026-10-15 文档整理：本文保留按日期演进的设计与实现记录。当前执行以 [项目规则](PROJECT-RULES.md)、[两阶段制作](SUBTITLE-TWO-PASS.md) 和 [定时任务](AUTOMATION.md) 为准；旧 accepted 前置、已看 experience、迅雷优先/UI heartbeat 或历史周期不自动恢复。

2026-10-16 日文来源漏检更正：按 [逐集日文来源](SUBTITLE-JAPANESE-SOURCES.md) 核本地轨、Jimaku和用户指定页的实际字幕正文；缺日文与候选待匹配、检索失败分别记账，交付明确普通对白与歌曲各自语言覆盖。学生会E2已取得4份候选，旧“缺可靠日文”不再作为未检索的结论；待目标匹配与实际审校不冒充完成。调度只交接材料，沿原唯一owner/晚到修订控制，不抢当前槽、不批量回修已看。

2026-10-15 用户最新更正：执行 [每集v0→v1双版本流程](SUBTITLE-TWO-PASS.md)。先上架v0观看版，紧接着由同一负责人完成v1精校细调；画面字、主动查找并制作OP/ED歌词、位置避让、必要动效及改写语义/时轴回查是每集v1必做项，不能为快整项省略。具体无解项留实际尝试和证据后归档，不卡几天。已看集保持原字幕，不自动回修。v0交付后不释放唯一writer，v1处理及输出完成才结束；保留两个观看入口。此条覆盖下文旧“首版即停止、只等新材料修订”安排，旧内容留作历史。

# 字幕及时交付与任务运行契约

2026-10-14字幕库进度回报：独立owner可使用 `progress JOB_ID --thread-id THREAD_ID --stage writing|layout|rendering|checking|publishing [--done N --total M]` 写实际当前阶段，只有确认的active owner/输入有效；不改变正文/验收/默认/观看。CLI控制命令同步安全公开只读视图，供新版字幕库显示素材数量、首版/修订版及下一步；原派发保护不变。详见SUBTITLE-LIBRARY.md。


2026-10-14 最新交付标准：用户明确要求效率优先、必须先有完整熟肉成品。执行 [熟肉交付流程](SUBTITLE-DELIVERY-FIRST.md)。完整可看版本交付后记 finished；原声争议和未完成的全面审校作为版本说明/后续修订，不再阻止本地成品交付或长期占用制作槽。下文旧“全部四阶段通过才算首版完成”的前置要求被本条覆盖，实际审校记录继续如实保留。

2026-10-14调度实施：首版 finished_v1 使用宿主持久 policy.first_release_mode=finished 加本集 readiness.dispatch_standard=finished_v1 双重选择；仍核完成视频/任务manifest/归属/黑名单、真实中文原稿SHA和sample-only实际原片检查。缺JA/全片实际听核转review_notes，不编造旧能力证书。delivery_capability_report须私有path/SHA、role=finished_v1_technical_preparation、show_id/episode/精确target_fingerprint、reviewer/evidence、未过期expires_at，以及实际technical_decode_passed/output_tools_verified/local_viewer_available；只证明首版输出可操作，accepted门禁未放宽。未选择此标准的旧任务仍按原材料规则。

单集成品实际上架并停止writer后新增命令：`scripts/subtitle_delivery_queue.py finished JOB_ID --thread-id RESPONSIBLE_THREAD_ID --release PRIVATE_RELEASE_DIRECTORY --player-report PRIVATE_CHECK_JSON`。宿主核同集finished清单、实际全部文件hash/尺寸、完整解码/时长/原音轨、实际播放器检查与字幕库index/视频绑定，才释放该唯一owner的槽。player报告role=finished_episode_player_check且绑定release_id/video_sha256/reviewer/evidence/errors=[]；真实通过才填play_pause_passed/seek_passed/download_passed/complete_chinese_coverage_checked。未知/committing/异owner禁止释放；不改accepted、episodes/default/watches。snapshot新增delivery_finished独立于已看finished，已交付首版不可重复begin；原停止魔王owner仅保留audit。


2026-10-14收尾：本轮流程重订已交付，精简研究聊天的 automation-4 已按用户要求暂停。automation-2 的材料准备、派发与跟踪职责保持；不得把研究暂停当作三项原声通过或自动恢复旧正文 owner。完整待审稿 v2 保留，正式整集验收仍待完成。

2026-10-13后续：原声能力优化已获本地实施授权，按[本地试验与迭代](AUDIO-REVIEW-LAB.md)由精简研究聊天持续推进，原automation-2只读取新证据和当前状态，不重复运行同一失败实验。用户已停止旧魔王正文大图聊天，禁止自动向其发送恢复或继承旧rollout/图片；保留当前工作稿/owner/未知保护。足够新证据到位后须经受控唯一owner接续才写正文，不以模型部署或异文自动恢复，不放宽四阶段。

2026-10-13用户最新要求：前提材料准备好后，单独一个会话持续完成一集；晚到熟肉/修订走V2。本文覆盖旧每小时轮转写正文的做法，质量与下载门禁继续。药屋、乱马E2已看完且已有manual finished，不再优先交付；旧稿保留为低优先制作经验样本。

## 执行层次

| 执行者 | 职责 | 结束条件 |
|---|---|---|
| 现有本机后台 | RSS/实际种子/下载与回执/完成归属/原稿归档、更新材料事实 | 每轮低成本准备完成；不做AI文字审校 |
| automation-2准备与派发 | 看变化、补可操作准备项、领取就绪集、创建/恢复唯一负责人、跟踪结果和V2 | 本次准备/派发/跟踪完成；不改逐句正文 |
| 独立本地单集会话 | 固定输入，全集语义→实际声画/片内文字→统一渲染/播放器→正常accept/空闲publish | 精确成品已发布，或具体无法继续的能力/材料阻塞 |
| automation-3想看选源 | 材料有变化时完整实评一部首集并选主备，之后稳定更新 | 当前实评/来源变化处理完成；不负责字幕正文 |
| Kimi辅助 | 单集负责人指定的差异、术语或具体疑点proposal | 交付可合并提案；没有生产正文/验收写入权 |

automation-2改为每15分钟检查一次变化和派发；它是当前工具能调用创建会话的调度入口，不宣称本机后台可以直接调用Codex会话工具。每轮无需重复全媒体hash或旧报告，只有新材料/版本/实际证据或负责人需要跟踪时做对应操作。开始生产后不用等15分钟继续下一小批：单集会话持续工作。

每集最多一个实际写入负责人；整体最多一集主动生产，另一集可做材料预备。明确blocked且负责人已保存、停止写入时可释放整体槽给不同集，原集仍保留原会话。未知派发、committing、有效租约或仍在写的会话始终保护，不能因超时重开。

## 当前落地接口

`anime/subtitle_delivery.py`保存独立调度表 jobs/events/late_inputs，`anime/subtitle_delivery_materials.py`只读核就绪。它们不调用下载器，不改episodes/watches，不冻结母片、不编辑书、不生成accept。所有默认结果由现有宿主再次验证。

* `GET /api/subtitle-delivery`：只读队列、负责人、阶段和下一缺口；不输出lease/dispatch token。
* `POST /api/subtitle-delivery/refresh`：用本机事实刷新；客户端ready布尔无效。沿原同源和会话令牌保护。
* `POST /api/subtitle-delivery/report`：负责人回报真实blocked或宿主已验证的accepted；同源令牌保护，不能靠声明生成验收。
* `scripts/subtitle_delivery_queue.py`：本机独立控制入口，不实例化Engine、不提交下载；当宿主路由尚未加载时也可使用。`status`严格只读；其它命令显式写调度表。

命令从D:/AnimeService运行，用本项目`.venv/Scripts/python.exe`：

```text
scripts/subtitle_delivery_queue.py status
scripts/subtitle_delivery_queue.py refresh
scripts/subtitle_delivery_queue.py begin JOB_ID --owner DISPATCHER_THREAD_ID --revision CURRENT_REVISION
scripts/subtitle_delivery_queue.py confirm JOB_ID --thread-id RETURNED_THREAD_ID
scripts/subtitle_delivery_queue.py unknown JOB_ID
scripts/subtitle_delivery_queue.py blocked JOB_ID --thread-id RESPONSIBLE_THREAD_ID --reason CONCRETE_REASON --stopped
scripts/subtitle_delivery_queue.py accepted JOB_ID --thread-id RESPONSIBLE_THREAD_ID --build-id VERIFIED_PUBLISHED_BUILD
```

以上不是要重跑的固定批次；先读实时JOB_ID/revision/状态。token只在数据库及进程内使用，CLI输出递归去除token字段。`--stopped`只能由已经安全保存并停止正文写入的负责人使用，不能清掉未知派发。已看经验样本领取须显式`--experience`，通常只在新未看队列没有可操作项时做。

## 什么是就绪

分别记录material_ready、clean_review_ready、capability_ready；范围无效blocked，材料/母片资格未齐preparing，齐料但能力缺失blocked，全部有真实依据才ready。派发前不要求四阶段已经接受；母片冻结、映射和全集验收在独立会话中实际完成。

材料：当前watching/selected与原授权/映射盘点/本集边界有效；已完成且归属/manifest/尺寸/当前指纹正确的目标媒体；真实不可变中文正文和可靠日语对白依据。只有音轨并无日语文字时需实际听写/核对通道，ASR仅候选。标签“简日”或jpn轨不能证明正文日语；SC/TC/重包装不是多个独立译者。登记记录另引用`japanese_dialogue_report`的path/SHA：本集实际盘点报告含status=verified、role=dialogue、coverage=whole_episode_dialogue、show_id/episode、reviewer/evidence及source_raw_hashes，与真实存档原稿SHA对应。歌曲-only/片内字/估算ASR不算对白；该盘点也不替代之后逐句语义和实际原声验收。

派发候选资格：真实private base_review与精确fingerprint、identity/fidelity及关闭软轨后实际代表性样帧事实；不在黑名单、不声明AI处理。可采用role=production_candidate、scope=sample_only、clean.status=sampled_clear及method=raw_sampled/soft_disabled_sampled，保存实际inspected_source_frames与证据。它只证明适合开启本集审阅，不宣称全片无烧字/广告。RAW/MKV/码率本身不直接放行。整集母片洁净、原声完整与正式freeze/accept的连续实看/实听门禁保持，在独立单集会话里完成，不能提前全部做完才派发。通常主/对照两份，不增下载名额。

能力：实际短场景已经听过声音、看过连续画面且非默认ASS播放器通道验证的私有报告，绑定目标hash和有效期限。样片能力只证明能进行工作，不关闭全集验收。登记键为`subtitle_delivery_readiness_<sid>_<ep>`，引用本地base_review/capability_report的path/SHA；尚未冻结时可另存已核completed研究候选的target（path/size/fingerprint），它只是就绪输入，不替换普通分集。真实能力报告须含reviewer、target_fingerprint、actual_audio_received_and_listened、continuous_picture_observed、non_default_ass_player_verified、evidence与expires_at。不得写合格布尔来绕实际审校。

2026-10-13魔王616808/E2已补425条真实日语对白原稿、已盘完成视频和中文基稿，本地实际音频输入/完整有序帧实看及新4896非默认ASS连续预览通道已实测；具体能力范围和失败在docs/SUBTITLE-REVIEW-CHANNELS.md，不声称全片认知/成品验收。学生会554779/E2继续材料预备，不占魔王生产槽。旧私有QA4893/4/5或旧muted/旧hash证据不复用；新材料齐后立即调度派发，无需逐集再征求许可。

## 派发操作：先持久意图，后外部会话

1. `refresh/status`读取当前事实和所有保护。优先新未看、门禁有效且ready；已看样本不占首交付位置。必要首集术语可读旧研究，不为此先补做已看整集。
2. `begin`按expected revision领取，持久committing和固定输入，再返回无token的session_prompt与action。不能先创建会话后才补台账。
3. action=create时用Codex `create_thread`，AnimeService已列出的projectId、本地environment、完整session_prompt；模型保持用户默认，不开额外worktree。用户本次明确授权就绪单集独立会话。
4. action=resume时只向responsible_thread_id发同集继续/V2消息，复用原会话；不新建V2聊天。每次外部派发结果未知，用`unknown`保护，先list/read/wait核实际会话与输入，不猜未创建、不重复发。
5. 创建/恢复成功后保存准确返回threadId，不使用clientThreadId代替。`confirm`登记回执；一次有界wait_threads读新状态，之后用cursor跟踪，active时不发送重复继续，不干扰用户阅读或观看。
6. 单集会话依 `SUBTITLE-SESSION-PROMPT.txt`和生产流水线持续执行。批次是内部保存点，不是结束一轮的理由。无法继续时列具体能力/材料、已完成证据和下一关闭动作；负责人安全停止后回报blocked。
7. 新证据确实关闭阻塞时，原会话重新竞争全局槽并继续；不按时钟反复唤醒同一无能力步骤。验收后既有受控空闲publish，宿主核真实默认/文件/字体/加载结果再记录accepted。

控制队列只有一集主动生产，不代表正在播放会被锁住；独立观看会话仍沿原播放保护，旧合格中文过渡版正常可看。原稿、旧媒体、分集、已看和外部/未知下载回执都保留。

## 晚稿与V2

每集仍重新发现原稿，先归档再按完整规范化文本比较。已派发/停止等待/已接受的工作稿由原负责人独占；后台仅记录pending_inventory，不把新候选写进当前书或换目标。

成品之后的新raw/text相对accepted基准自动排V2：同文只存新来源，不另开审核；不同正文合并成一个待审差分包；正在审的V2输入不变，继续到来的材料进入下一包。审核复用同集会话，输出proposal和受影响证据，不重复全文旧审校。无实质问题时先保存本次差分已审来源hash/理由的no-change报告，然后对该late JOB使用`accepted --build-id 当前baseline_build_id`完成回报；宿主验证旧成品仍确实合格，释放V2槽，不生成新build或伪造新accept。有真实改进才出新版本，重验受影响语义/声音/时轴/视觉，并重新绑定当前全书证据，空闲发布；旧成品在此期间继续可用。

没有首版成品的在途新稿仅暂存，以首版负责人处理当前明确疑点；不因新稿不断到来重开全集或无限延迟交付。

## 通知与计时

只通知材料就绪后首次派发、新未看集真实成品、首次影响交付的实质失败或明确用户操作需求；等待、重复能力缺口、无变化和同文晚稿安静且指纹去重。已看样本不当新观看成品通知。

jobs/events保存派发/回执/阻塞/完成时间；单集run另记各阶段实际开始/结束、机器执行与审校会话、等待和返工，不把派发到结束的日历跨度当纯工时。以“成品是否在观看前到位”和首稿到交付时间衡量，截图数和工作稿比例不是交付效率。

调度工具依据本机可调用schema和配置回读；[OpenAI Scheduled tasks](https://learn.chatgpt.com/docs/automations)说明周期入口可以保持原聊天或单独任务，本项目采用准备heartbeat＋独立整集会话。工具/网络/权限或审校模态不可用会阻塞真实成品；本次不购买外部模型或自动外传音视频来掩盖缺口。


## 完整待审稿授权（2026-10-13最新）
用户已明确先交完整可看待审稿，原声等未核内容单列，不冒充验收。允许从既有书完整快照另存私有review分支，不以三项声音accept作为该分支导出前提。私有待审包单writer、固定source/hash/原稿保留；不写原共享书、不改旧production owner或未知派发/lease，不直接default/accepted/publish。当前精简chat已交付魔王E2 review-draft-20261013-v2，正式质量门禁保持，任务不重复派发或唤醒旧大图正文。
