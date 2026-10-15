> 2026-10-15 文档整理：本文保留按日期演进的设计与实现记录。当前执行以 [项目规则](PROJECT-RULES.md)、[两阶段制作](SUBTITLE-TWO-PASS.md) 和 [定时任务](AUTOMATION.md) 为准；旧 accepted 前置、已看 experience、迅雷优先/UI heartbeat 或历史周期不自动恢复。

# 本地字幕声画审校通道

2026-10-13后续更新：用户已委托实际实施与持续优化，新增隔离`audio_review_lab/`，详见[本地原声试验与迭代](AUDIO-REVIEW-LAB.md)和私有`data/subtitle-studio/audio-review-lab/CURRENT-STATE.json`。现有Whisper VAD对照、Qwen1.7B原生、Gemma原始FP32及MossFormer2短窗均实际运行；没有把安装/高精度/分离长度匹配升级为声音验收。重点叠声/读音/第二次技能仍未关，OP/ED与平台时轴缺口保持。旧正文大图聊天由用户停止，不自动唤醒；研究在精简新聊天继续，正文接续须受控唯一owner迁移。当前实际模型观察仍不能冒充Codex亲听。

2026-10-13，魔王616808/S02E02实测。通道就绪只是可以开始审校，不能代替整集验收。当前Codex提供文字/图片输入；实际音频观察交给已经安装的本机音频模型，完整有序帧由Codex图片模态实看，连续播放器另测。不得声称Codex亲听或原生视频输入。

## 日语正文

本集ABEMA声明的公开CC源425条已不可变归档：`85e28d5ac7f743fc66484834025af665`，原SHA `a83a37df2fec353852feb7ac3aeb3ac71a5b2a39e3ed5c1297328158af3df67f`。官方第14话标题对应本地第二季第2集，不能把公开文件S02E14当本地第14集。来源/原句/8处时间对照在`data/subtitle-studio/maou-e2-ready-20261013/japanese-discovery/`。

原CC提取链是贡献者声明；有故事对白，歌曲缺失。CR目标与ABEMA原时码有变化，不能统一减168ms或伪装同媒体日语。先作语义参考，目标映射必须逐段对原声/目标PTS验证。

## 原声输入与审校

`anime/subtitle_audio_review.py`的`LocalAudioReviewer`只访问独立`127.0.0.1:11436`，使用已安装`gemma4:e2b`，通过`/v1/chat/completions`的`input_audio`输入PCM16/mono/16kHz、每段≤30秒。服务为本次已核隐藏隔离进程，不动11434、下载器、生产Engine；不pull、不调用云、不外传私有音频。

```python
from anime.subtitle_audio_review import LocalAudioReviewer
reviewer = LocalAudioReviewer('gemma4:e2b')
cap = reviewer.capability()
observation = reviewer.observe(wav_path, focused_question,
                              expected_model_digest=cap['model_digest'])
```

用本机既有ffmpeg明确选择JPN音轨生成原声窗，保存完整源SHA/流号/请求起尾/样本数/WAV SHA。整集连续窗与重叠边缘需覆盖表；切句子再检查前后上下文，不拿请求起尾当已听到的边界。每个问题单独问原文、否定、主体、声部或重叠，先盲问不把参考字幕喂成答案，再比较真实CC/中文。词、语气、声部、时值的漏答或矛盾必须具体留待复核，不用同模型多数票关闭。

真实音频正样、静音/无输入负样证明通道确实收到音频；魔王469–479秒聚焦问题识别`できない`否定。E2B同音汉字、声部和精确起尾错误仍存在；E4B盲控及实际片段失败，不能因更大模型改用或写通过。所有返回都是`proposal_only`，原始问题/输入hash/模型digest/响应/用时保留。本地模型观察不是人类或Codex亲听，不能单靠转写生成全片声音验收。

报告：`data/subtitle-studio/maou-e2-ready-20261013/audio-capability/AUDIO-CAPABILITY-REPORT.md`。冷加载首请求可能60秒超时；保存失败并确认现有服务/模型后再暖测，不启动第二实例、不循环重试。

## 连续画面

已装Gemma E2B/E4B虽然有vision标签，盲运动/遮字控制失败，不能用于本轮运动验收。采用可复核的完整连续解码帧序列：`data/subtitle-studio/maou-e2-ready-20261013/visual-capability/ordered_frame_review.py`的`prepare_ordered_review(video, output, source_provenance, width=640, columns=3)`，每个短窗最多60native帧，超量拒绝而不是抽样。保留每帧PTS、time_base、原/缩图SHA、实际fps、无漏帧检查、片段与源绑定；受限脚本只生成证据，不生成observer/accept。

实看全部有序图，必要时逐张打开原尺寸或连续分页，记录动态方向、进入/退出、主体与文字遮挡以及具体帧。不要把长contactsheet缩图当逐张1920字体验收，不把有序帧观察称为实时观看或原生视频理解。短窗是能力证明；整集检查必须有覆盖/接缝记录，风险段回到真实源PTS，而不是几个远隔静帧证明全片。

## 非默认ASS连续预览

`anime/subtitle_preview.py`提供独立loopback服务和`prepare_clip`，不启动第二Engine，不写观看/default。当前4896服务本集目标与资源白名单/hash有保护，私有地址仅`preview-engineering/connection-local.json`内，不能打印随机权限路径。

现有H264/AAC预览保留原片，浏览器canvas-only libass-wasm以rVFC实际mediaTime同步ASS，重建640/1920尺寸，暂停seek和控制条显隐可测。渲染诊断两行是假句，用于通道测试，不是本集字幕。可用真实中文参考查看原轨，但不能当自制成品。`preview-channel-proof-final.json`和`fresh-cold-primed-01/headless-report.json`保存修复后全新context的真实caption像素、帧推进、当前资源/hash；旧失败历史保留。原播放器字体实际选字和完整听看仍需当前成品实测。

初版冷启time0的native loading弧已实际修复：QA-only静音解码至少两帧、pause再seek0，最终time0/paused/muted；全新context的640/1920截图无黑弧，六case及八播放图再测通过，旧失败保留。这只是通道修复，不是本集整片播放器接受。要换本集ASS或片段，准备新的不可变QA目录并沿同目标守卫启服务，不覆盖旧证据、原媒体，不把QA衍生编码选作正式母片。

## 单集继续

魔王的三张初中末纯raw图只证明候选可以进入整集实评：前中无烧录对白，末尾为原片ED制作字；没有全片clean、时轴、声部或字体accepted。独立会话从可靠语义工作开始，持续完成母片整片实评、受控freeze、目标映射及四阶段；具体认知失败通过更短原声窗/完整帧/独立材料解决，确实无能力才保存准确blocked。不要重复准备统计或将短场景证书复制成全集接受。
