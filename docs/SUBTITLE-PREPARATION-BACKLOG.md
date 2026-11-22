2026-10-22 无人工制作：执行 [中文字幕来源与无人工作业](SUBTITLE-CHINESE-SOURCES.md)。Codex 自动检索并核对同集熟肉软字幕/硬字幕 OCR；已有全片 OCR 直接校对与跨版对轴，不能因独立稿搜索失败而停止。字幕缺稿、待转写或待校对不转人工/用户介入；来源候选与实际可用主稿、ready/finished/accepted 分开，中文防漏及唯一 owner 等保护不变。本条覆盖下文旧人工前置。

2026-10-22 并行更新：用户明确要求多会话同时处理字幕积压，执行 [SUBTITLE-PARALLEL.md](SUBTITLE-PARALLEL.md)。整体改为显式容量最多三集，不同集并行，每集/每线程仍唯一；片源、原稿准备独立推进。下文历史单槽描述由本条覆盖，已看、未知回执、owner 与质量保护不变。

# 在追字幕准备不能静默积压

2026-10-21：按 [SUBTITLE-RECOVERY.md](SUBTITLE-RECOVERY.md) 执行持久失败次数、冷却、超时实修和去重介入通知。“同条件不重复研究”不再意味着永久等待；先走已授权的安全修复，同路线最多两次失败后切换仍可行动的路线；仅真正需要用户操作的独立事项升级，字幕材料/核对留自动准备。原五分钟守卫记录准备健康、原十五分钟调度实际执行，不增设重复任务。

An observed episode was watched before custom-subtitle work ever began. Retained material and inventory did not establish a usable Chinese dialogue base; this is a preparation failure, not a reason to claim that watching caused the original absence of production.

`subtitle_delivery_queue.py status`现在返回只读`preparation`，单列本地视频已到但仍需准备的未看集、等待媒体、受保护范围，以及有历史材料盘点时间证明的“交付前已看”。最后一项只用于流程审计，不授权回修、experience或更改观看记录。有实际盘点记录且等待超过6小时的准备项标attention_required；不以每轮refresh的updated重置等待，也不靠当前文件存在猜下载完成历史。这个阈值是处理提醒，不是完成时限或质量豁免。

原automation-2每轮先有界核当前owner，然后必须从available准备池处理一个未看、未派发、范围有效的作品，即使主制作槽正忙也执行。按最近实际preparation_attempt轮换作品，不固定挑同一“下一集”。同一作品先处理较早可用集；刚失败且没有新材料/新条件的项不重复研究，记录下一所需证据后轮到其它作品。不要用改标签、重复refresh或读同一旧报告当准备动作。

当轮既有closure.json增加preparation_attempt：job_id、input_key、action、实际证据路径/SHA、result、next_condition、checked_at。旧记录没有该字段不算做过；同一失败必须区分已尝试与尚未处理。发现缺中文原稿时执行 SUBTITLE-CHINESE-SOURCES.md，核所有已登记同集熟肉/外挂/软轨与现有全片 OCR，先消化已有 OCR，再补查独立来源；Codex 自动逐项对照源帧、校正并映射到目标。熟肉硬字幕可形成主稿，不因缺软轨或独立稿就排除；Multi-Subs、jpn音轨、下载完成或未核 OCR 本身不满足可用主稿门禁。日文仍按SUBTITLE-JAPANESE-SOURCES.md实际查找；v0缺可靠日文可以中文先交，不能编造对白。

准备检查不领取正文writer、不直接写DB、改固定input、清未知回执或重投下载。真实readiness仍沿原宿主控制流程登记；就绪后begin/原票据/唯一owner/confirm保持。当前owner的v0→v1连续制作保持，准备轮换不抢它。字幕库使用真实积压提示，不承诺尚未齐料作品的开工时间。
