# 补充字幕来源与核验状态

核验日期：2026-10-16（Asia/Shanghai）。优先服务既有在追逐集的日文对白缺件，同时列出中文参考入口。站点存在/分类可读不等于某集已下载、可用或来源可靠；实际字幕仍执行 [来源及目标匹配流程](SUBTITLE-JAPANESE-SOURCES.md)。

| 站点 | 可用于什么 | 本次实际核验 | 使用边界 |
| --- | --- | --- | --- |
| [AJATT](https://subtitles.ajatt.top/) / [GitHub镜像](https://github.com/Ajatt-Tools/kitsunekko-mirror) | 日文动画、剧集SRT/ASS及Kitsunekko备份 | 完整目录HTTP200，找到学生会专页；实际下载本集4份原稿并计算SHA | 4份与Jimaku逐字节一致，增加备用入口，不增加独立稿件数 |
| [Kitsunekko日文目录](https://kitsunekko.net/dirlist.php?dir=subtitles/japanese/) | 日文字幕目录补查 | 本机HTTP200，完整目录已读 | 本次未核到本集文件，不能声称有E2或全季；网页工具超时不等于站点离线 |
| [Anime Tosho](https://animetosho.org/about) | 查已处理发布包抽出的字幕附件 | 官方FAQ实际读取，明确包含字幕提取；本作检索页HTTP200 | 尚未核得本集日文附件；发布范围偏英语动画，不能把MultiSub或MKV当有日文，不因此重下视频 |
| [SubDL日文分类](https://subdl.com/fr/latest/japanese) | 综合日文字幕补充检索 | 分类页实际可读，列有Japanese条目 | 本轮未验证文件下载；日文也可能是外语影视的译文，不能当动画原始日语。用网页现成字幕，不调用AI翻译/转写服务 |
| [ASSRT](https://assrt.net/) | 中文/双语字幕与译稿对照 | 网页首页实际读取；本机另一HTTP通道403 | 本轮未验证具体下载或日文对白，按通道记录访问状态，不宣传为原生日文主站 |
| [SubHD](https://subhd.tv/) | 中文/双语字幕与译稿对照 | 本机HTTP200取得完整首页；网页工具403 | 具体文件及下载要求待逐集核，不绕过验证，不把双语标签直接认作中日 |

另行保留但不列为当前可自动获取：OpenSubtitles（.com/.org本轮403）、JPSubbers目录（握手超时）；不是判定永久不可用。Animelon读到学习播放器/字幕工具页面，但未核一般外挂字幕导出，因此不充当已验证的下载站。网页显示为日语不证明字幕语言就是日语。

## 实际本集备用入口与去重

[AJATT《学生会也有洞！》专页](https://subtitles.ajatt.top/anime_tv/seitokai-ni-mo-ana-wa-aru!.html) 提供GitHub raw下载。本次只下载既有授权的554779/S01E02四份，不取其它集：

- ABEMA标签SRT：35576字节，SHA与Jimaku原稿完全相同。
- Netflix标签SRT：38123字节，虽文件名空格/下划线不同，字节SHA相同。
- Judas ASS/SRT：79427/47851字节，SHA各自与Jimaku对应文件相同，ASR来源声明继续保留。

“多个站都有同一稿”用于可用性备份，不作为第二译者或原声正确的票数。Jimaku帮助页也说明部分来源于Kitsunekko/JPSubbers，不能把镜像当独立平台抓轨。

## 后续任务采用

automation-2/4及单集会话已引用SUBTITLE-JAPANESE-SOURCES.md，因此沿共享流程使用本清单，无需改周期或新增定时任务。每集先查现有有效材料/用户线索与主入口，缺件才有界补查最相关备用站；故障记录后退避，不在同轮重试全站，不整库克隆。不扩大作品/季集、不新增付费/API密钥、不外传原声/视频/私有稿，原owner/已看/媒体及活动播放保护保持。

本次AJATT材料没有新增独立正文，不因此重复排制作/重复给active writer发继续，原本集待目标匹配任务保持。

私有证据：`data/subtitle-studio/subtitle-source-research-20261016/` 内HTTP探测、实际目录/本作页、4份原文和`ajatt-download-verification.json`。部分通道结果不同已分别保留。目录查阅、网页能力声明和实际文件下载是不同证据层级。
