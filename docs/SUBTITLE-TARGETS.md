> 2026-10-15 文档整理：本文保留按日期演进的设计与实现记录。当前执行以 [项目规则](PROJECT-RULES.md)、[两阶段制作](SUBTITLE-TWO-PASS.md) 和 [定时任务](AUTOMATION.md) 为准；旧 accepted 前置、已看 experience、迅雷优先/UI heartbeat 或历史周期不自动恢复。

# 研究母片冻结与正式播放绑定

这是 VIEWING-WORKFLOW v1 的受控宿主路径。研究母片与现有中文过渡视频分别登记，整集验收与正式发布分开。代码通过临时数据库和原创合成文件测试，不表示任何真实作品通过验收；实际服务加载状态以最新 PROGRESS/HANDOFF 为准。

## 冻结

`GET /api/subtitle-studio/{sid}/{ep}/production-target` 返回当前 target_id / fingerprint，不暴露私有路径。`POST` 同一路径指定 fingerprint、base_id、expected_target_id、base_review `{path,sha256}`，必要时附 alignments。只能使用当前 watching、selected 且原授权/映射/盘点/本集门禁有效、已完成且任务 manifest 精确归属的媒体。

base_review 是私有字幕目录内的不可变 JSON。必须绑定 target_fingerprint、reviewer；identity / clean / fidelity / content_audio 每项记录 status=verified 和实际 evidence。clean.method 为 raw_continuous 或 soft_disabled_continuous；content_audio.actual_listening=true 只在确实听核原声及完整剪辑后记录。标签、导出、ASR、静帧和模型生成的字段不能作为这份实际证据。缺这些事实时继续语义工作稿，不能调用冻结。

新目标的工作稿由原始中文基稿创建，跨版本须真实已核时间映射，不复制旧目标的接受状态/时轴证据。相同目标已有稿保留。旧目标记录、过渡分集、原稿、媒体、观看状态和原工作稿全部保留。expected_target_id 冲突须重读，不盲目重投。冻结成功但摘要刷新失败会明确返回 job_summary_pending；先 GET 当前 target_id 及真实工作稿，不重复冻结。

刷新作业优先冻结目标。冻结媒体变化会拒绝刷新并保留旧稿，不根据新文件大小/指纹自动重新绑定。

## 验收与发布

正常研究项目 compile/accept 完成后，`POST /api/subtitle-studio/{sid}/{ep}/publish` 指定 target_id、build_id，以及私有 player_review / idle_review `{path,sha256}`。接受成品的 decisions 必须与当前工作稿完全一致；精确目标、成品/字体字节、当前黑名单保真及源稿均再次核实。

player_review 必须绑定 build_id、target_fingerprint、status=verified、reviewer、evidence 及 actual_playback=true。这些字段仅保存真实目标播放器观察；不能为预览先 accept。idle_review 必须绑定 show_id/episode、status=verified、reviewer/evidence、web_idle=true、external_player_idle=true 和 observed_at（60秒内）；未知外部播放状态不能填写空闲。

暂停/后台浏览器页也每30秒续期会话，租约180秒。近期旧客户端播放进度另外保守保护5分钟。有会话时返回 waiting_idle，不切换正式分集。只有整集质量及空闲证据都已通过，才在单事务中保留旧分集完整快照并发布新视频/成品绑定。接口幂等，不复制跨剪辑播放位置或 finished。

旧媒体 URL 继续读取保留的原媒体，历史构建/字体仍验证当前字节及来源排除。研究项目的新 accept 不直接成为冻结目标的播放默认；只有正式 publication 允许其供播放器加载。新组合下次打开生效，没有前端强制刷新事件。

## 加载和接续

新增表：subtitle_targets、subtitle_target_heads、subtitle_publications、subtitle_viewing_leases。随既有唯一宿主实例正常加载创建；不另起生产 Engine/uvicorn，不直接操作下载器。未确认网页和本机播放器空闲时，不为加载代码强行重启服务。

本路径只是工程门禁。现有实际听音/连续运动/目标播放器能力未满足的作品仍阻塞相应质量阶段，保持现有合格中文过渡版。本轮没有实际冻结/正式发布，也未迁移任何真实工作稿。
