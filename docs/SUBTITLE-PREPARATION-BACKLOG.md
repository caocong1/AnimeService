# 在追字幕准备不能静默积压

An observed episode was watched before custom-subtitle work ever began. Retained material and inventory did not establish a usable Chinese dialogue base; this is a preparation failure, not a reason to claim that watching caused the original absence of production.

`subtitle_delivery_queue.py status`现在返回只读`preparation`，单列本地视频已到但仍需准备的未看集、等待媒体、受保护范围，以及有历史材料盘点时间证明的“交付前已看”。最后一项只用于流程审计，不授权回修、experience或更改观看记录。有实际盘点记录且等待超过6小时的准备项标attention_required；不以每轮refresh的updated重置等待，也不靠当前文件存在猜下载完成历史。这个阈值是处理提醒，不是完成时限或质量豁免。

原automation-2每轮先有界核当前owner，然后必须从available准备池处理一个未看、未派发、范围有效的作品，即使主制作槽正忙也执行。按最近实际preparation_attempt轮换作品，不固定挑同一“下一集”。同一作品先处理较早可用集；刚失败且没有新材料/新条件的项不重复研究，记录下一所需证据后轮到其它作品。不要用改标签、重复refresh或读同一旧报告当准备动作。

当轮既有closure.json增加preparation_attempt：job_id、input_key、action、实际证据路径/SHA、result、next_condition、checked_at。旧记录没有该字段不算做过；同一失败必须区分已尝试与尚未处理。发现缺中文原稿时实际核本地轨、独立字幕文件/已授权来源和可用校对路径；Multi-Subs、jpn音轨或下载完成不满足它。日文仍按SUBTITLE-JAPANESE-SOURCES.md实际查找；v0缺可靠日文可以中文先交，不能编造对白。

准备检查不领取正文writer、不直接写DB、改固定input、清未知回执或重投下载。真实readiness仍沿原宿主控制流程登记；就绪后begin/原票据/唯一owner/confirm保持。当前owner的v0→v1连续制作保持，准备轮换不抢它。字幕库使用真实积压提示，不承诺尚未齐料作品的开工时间。
