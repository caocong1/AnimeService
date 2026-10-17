# 项目文档目录

本次整理日期：2026-10-15，时区 Asia/Shanghai。文档中的旧验收数量、作品状态和历史任务周期仅说明当时事实，当前状态须读实时台账。

## 阅读顺序

1. [README](../README.md)：能力、隔离开发和安装边界。
2. [当前项目规则](PROJECT-RULES.md)：当前工作约束和新旧政策优先序。
3. [项目经验](PROJECT-EXPERIENCE.md)：为什么这样做、哪些证据不能互相替代。
4. [定时任务](AUTOMATION.md)：谁准备、谁制作、谁复盘，以及中断后的恢复。
5. [开发与部署](DEVELOPMENT.md)、[Git 整理范围](GIT-ARCHIVE.md)：代码入口、测试与保存边界。
6. [Claude跨电脑研究](CLAUDE-SUBTITLE-RESEARCH.md)、[问题基线](SUBTITLE-RESEARCH-BASELINE.md)：另一台电脑的执行方式、完整提示词和研究回传。

## 字幕与观看交付

| 文档 | 用途 |
| --- | --- |
| [两阶段制作](SUBTITLE-TWO-PASS.md) | v0 后立即继续 v1；六类必做工作、已看保护和版本含义 |
| [交付优先](SUBTITLE-DELIVERY-FIRST.md) | finished 与正式 accepted 分离；完整可看的首版先交付 |
| [单集派发](SUBTITLE-DELIVERY.md) | 就绪判断、持久意图、唯一会话、晚稿增量 |
| [单集提示词](SUBTITLE-SESSION-PROMPT.txt) | 单集负责人的执行入口 |
| [生产流水线](SUBTITLE-PRODUCTION-PIPELINE.md) | 归档、母片、审核、冻结与受控默认发布 |
| [字幕库](SUBTITLE-LIBRARY.md) | 真实阶段、成品入口、首页/分集接线与续播 |
| [字幕工作室](SUBTITLE-STUDIO.md)、[目标工程](SUBTITLE-TARGETS.md) | 工作稿、宿主绑定及工具边界 |
| [审校通道](SUBTITLE-REVIEW-CHANNELS.md) | 实际可用能力与未通过的能力 |
| [原声研究](AUDIO-REVIEW-LAB.md)、[音频对齐可行性](AUDIO-ALIGNMENT-FEASIBILITY.md) | 隔离实验、控制样本、失败与证据边界 |
| [遗留问题](SUBTITLE-ISSUE-REVIEW.md) | 具体问题归档及有新条件才验证 |
| [日文来源核查](SUBTITLE-JAPANESE-SOURCES.md)、[来源目录](SUBTITLE-SOURCE-CATALOG.md) | 逐集实际查源，区分候选、故障、映射和原声审校 |
| [字幕自动登记](SUBTITLE-ENROLLMENT.md) | 已授权在追作品幂等补登记，不扩大授权或抢owner |

## 选源、播放器与集数

| 文档 | 用途 |
| --- | --- |
| [观看工作流](VIEWING-WORKFLOW.md) | 想看首集实评与在追字幕制作分流 |
| [原片索引](NATIVE-SOURCES.md) | 索引、身份、季号、保真、torrent 与下载保护 |
| [集数身份](EPISODE-IDENTITY.md) | 本地集号、累计编号、季与作品归属 |
| [Bangumi](BANGUMI-PLATFORM.md) | 资料、章节和单向个人记录同步 |
| [弹幕对齐](DANMU-ALIGNMENT.md) | 对齐依据和失败边界 |
| [画面设置](WEB-PICTURE-QUALITY.md)、[网页播放研究](WEB-PLAYBACK-RESEARCH.md) | 播放链路与兼容性 |
| [片段导出](CLIP-EXPORT.md) | 导出功能及权限边界 |
| [迅雷队列历史设计](THUNDER-WORKER.md) | 保留队列与未知提交保护；当前使用 qB 后台优先 |
| [调度健康](CODEX-DISPATCH-HEALTH.md) | 实际卡住诊断与受限本机守卫 |

旧文档可能同时含有按日期追加的更正。发生冲突时，执行用户当前明确授权和 [当前规则](PROJECT-RULES.md)，不要凭旧章节恢复 UI heartbeat、逐句轮转或首版无限研究。原始 `AGENTS.md`、`PROGRESS.md`、`HANDOFF.md`、`DECISIONS.md`、`REQUIREMENTS.md` 及私人实评台账继续保留在本机；可复用结论已整理入本目录。
