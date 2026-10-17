# 每集 v0 观看版 → v1 精校细调版

2026-10-16 日文材料核查补充：执行 [逐集日文来源](SUBTITLE-JAPANESE-SOURCES.md)。准备、v0转v1和缺稿归档前实际检查Jimaku/用户给定页及本地轨，保留下载/来源与匹配证据；候选未映射不记成unavailable，机器稿不当原生CC。中文首版可先交但必须明示对白日文缺口；v1须实际检索/处理可用日文材料，歌词双语和精校标签不证明全片双语。新材料走原owner的不可变增量修订，已看与活动播放保护保持。

2026-10-15 用户最新授权：速度不能靠省掉画面文字、OP/ED歌词、字幕位置避让和必要动效。每集先交当前标准的v0，紧接着由同一负责人完成v1；已看集保持原样。本文件覆盖旧“首版交付即结束、四项仅等新材料再做”的安排。finished是观看交付，v1不是伪造全片亲听或正式accepted。

## 两个观看版本

- **v0观看版**：复用可靠中文基稿和同媒体时轴，连续校订明显误译、遗漏、名字/数字/否定，完成基础可读排版、全片输出、解码及实际播放/跳转/下载检查。有可靠日文才双语。通过即上架，用户可直接看。
- **v1精校细调版**：v0上架后立即继续，不等下一次heartbeat，不把整项制作当成无期限研究。必须处理下列六项，逐项提供实际工作及证据；真实不能解决的具体项归档后继续其它项。处理完成且技术/播放器检查通过，即交付v1；v0继续可选。
- 界面中的edition=v0/v1表示制作层级；磁盘version表示不可变产物修订序号，两者分开。新集通常version=0/1。历史基础稿目录已叫v1时原ID、文件、hash、链接不改；未看集在观看索引标为v0，后续新目录用version=2、edition=v1，不覆盖旧版。没有六项工作证据不能只改标签冒充v1。

## v1每集必做

1. **画面字screen_text**：检查完整场景覆盖，包括标题、人物/技能卡、短信/纸张/告示和影响剧情的拟声字。按场景切换取代表帧，疑似文字处补区间帧；OCR仅定位候选，译文以实帧为准。记录出现/消失范围、原文、中文、位置和取舍；长卡片结合真实展示范围控制阅读负担。不能只核原稿已有的文字。
2. **OP歌词op**、3. **ED歌词ed**：实际确认本集歌曲/TV剪辑版本，主动查现有轨、已存歌词缓存及公开可靠歌词/官方材料，保存来源及实际歌词，补歌词翻译、句级时轴和中文上日文下排版。可靠歌词不足时保留可核部分，不编造；缺歌词写unresolved及具体查过的来源，不能标“不适用”。本集确无某段歌曲才可not_present并给实际证据。同曲可以复用已核歌词/译文，但本集剪辑和起尾要核，不能继承验收。没有可靠逐音节时值时采用实际核过的整句歌词，不做猜测卡拉OK。
4. **位置避让placement**：依据当前画面文字/脸/嘴/关键动作安排位置；先稳定字幕区域，只在实际冲突处调整。中文上日文下，叠声分区；全尺寸和640px实看亮暗背景、长句及切换。原稿竖排、旋转、剪裁、移动标签先解析其意图，不能换普通字体名后直接保留旋转；发现冲突用新布局修复。
5. **必要动效effects**：字卡/标牌随镜头移动、逐步揭示或短暂切换导致静态字幕不合用时，实际制作或修复相应移动、显隐、淡入淡出、跟踪或遮挡处理。检查整个事件区间和切镜。对白默认稳定，装饰性跳动不是必做；歌词逐字高亮仅在可靠歌词/时值支持时做。确无需要动效的场景可not_present，但需场景检查证据，不能把所有原特效默认为可丢弃。
6. **语义与时轴semantics**：回查全部非排印改写是否改了修饰关系、主语、否定、数字、双关和语气；检查中文字幕覆盖、读速风险及对白/字卡/歌词对应场景。高风险处用原声/可靠日文与上下文核对，ASR只作辅助。中日歌词肯否冲突、错行、片内字显示过短必须查。没有实际亲听能力就如实记录，不能生成声音accept。

## 效率与疑难退出

材料齐时v0目标60–90分钟，随后v1常规工作预算60–90分钟，分别记录校订、编码、等材料和返工；这些是预算，不是强制跳过任务的截止时间或无条件工时保证。按场景集中取帧/读原稿，一次建立画面字与歌曲清单，复用词表、字体、已核歌词和未变输出检查。优先处理所有必做类别，再深挖单点；歌词首次主动查找和各项首轮实际检查必须发生。

具体疑难首轮通常限10分钟、同条件只试一次；没有新证据就记录已尝试方法、现版处理、证据、下一新条件及是否影响观看，存入统一issues.json，不轮换模型/重复安装/反复整集重编码。需要长研究的单点交周复盘；v1其余项继续，不允许整个画面字/OPED/避让/动效类别因“缺能力”未经处理就省略。可靠材料不足的歌词保持未决，版本说明如实列出；有新证据再出不可变修订。

## 唯一负责人和任务控制

每集仅一个正文writer，整体仅一集主动制作。未来新集同一会话执行v0→v1，v0上架后调用原finished命令时，最新双版本政策返回continue_refinement=true，工作项仍production/acknowledged，保留原负责人、v0观看入口和独立v0回执，立即继续v1。v1经六项报告和实际成片检查后再调用finished，才结束writer释放槽。未知派发、有效租约、异owner和活动播放继续保护。

已有基础成品且未看、原任务finished/completed的集，用`refine episode:作品ID:集号`建立去重的refinement工作项，复用原会话，不能重新翻译整集。refresh也会按新政策补这些工作项。begin仍先持久意图，再只向返回的responsible_thread_id恢复；不得向active会话反复发继续。旧unknown/committing任务不因迁移而唤醒或抢占。对明确blocked-stopped且owner_stopped_writing=true、已有hash绑定可看成品的未看集，最新用户修订授权允许另建独立私有refinement任务，isolated_owner_required=true、thread_id留空，由调度begin/create/confirm建立新负责人；旧任务和停止聊天完全保留，不唤醒、不读取其rollout/图片、不写旧共享稿。未能验证基线或停止状态时保持具体待核。

领取、继续、输出前都核watches.finished；已看不入v1队列，不以experience绕过。如果制作中变已看，负责人保存私有进度，报告blocked --stopped及“已看，细调暂停”，不继续改字幕。未看不是仅看shows.watched总数或打开播放器推断，保留手动已看优先。周复盘对已看集可保留档案、查公开技术，但不改字幕或唤醒writer，除非用户点名。

进度阶段：writing、screen_text、lyrics、placement、effects、refining、layout、rendering、checking、publishing。只报告真实当前阶段。字幕库并列呈现v0可看、v1制作中/可看，v1完成后默认入口优先v1，同时保留“播放v0”；活动播放器不被强制切换。

## v1证据及工具

输出配置保留原媒体/ASS/字体SHA，增加edition=v0或v1。v1须有baseline_release_id和refinement_review文件路径。报告role=episode_v1_refinement_review，绑定show_id、episode、source_media_sha256、baseline_release_id、reviewer；sections为screen_text/op/ed/placement/effects/semantics。每项包括status=done|unresolved|not_present、work_done、evidence_refs=[{path,sha256}]；证据存私有subtitle-studio。unresolved另有issue_ids，必须实际存在于同集统一档案且blocks_delivery=false。placement/semantics不可not_present。

finish_subtitle_episode.py在编码前检查报告，随包保存refinement-review.json并纳入不可变文件hash；publish保留edition供观看索引读取；宿主finished再次核文件、报告、实际播放器回执与基线。报告检查只证明工作记录和绑定完整，不会自动证明译文正确或四阶段accepted。真正全面验收仍沿原freeze/accept路径。

当前抽查新证据在data/subtitle-studio/quality-audit-20261015/REPORT.md；仅未看对应集的v1负责人吸收，不回头改已看集。


## 2026-10-15 后续每集上架与入口核验（用户明确要求）

每一集v0/v1都必须通过`publish_finished_episode.py`登记统一成品清单，自动接入字幕库、追番首页/全部追番和作品分集播放入口。仅编码完成或只挂字幕库不能报告观看交付完成。单集负责人须只读核本集首页/分集状态不再错误缺集、按钮实际可打开；v1优先且v0保留，不能伪造accepted或修改原片/default/已看来凑状态。

已打开观看列表在可见时每60秒及重新显示/聚焦时自动检查清单与文件，仅有变化才更新观看区域；分集页未保存表单保持，活动播放器不切换。新页面直接读取最新成品，无需服务重启或逐作品手工配置。暂时网络/5xx故障保留当前显示并重试，文件实际404则不能新增播放入口。具体实现与核验见`static/finished-releases.js`、`scripts/verify_finished_auto_delivery.cjs`与`data/finished-viewing-repair-20261015/auto-delivery-proof.json`。
