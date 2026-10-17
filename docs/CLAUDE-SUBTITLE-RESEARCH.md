# 在另一台电脑用 Claude Fable 研究字幕问题

2026-10-17。推荐 **Claude Code + 独立研究分支 + 脱敏问题基线 + 按需本地证据**。
Claude 可直接沿代码追踪、运行隔离测试、保存实验和补丁；原机继续正常交付。
这次同步不迁移生产 DB、下载授权、自动化、制作负责人或观看状态。

## 拉取并开始

新电脑首次使用：

```powershell
git clone https://github.com/caocong1/AnimeService.git
cd AnimeService
git switch -c research/claude-subtitles-20261017
claude --model fable
```

已有干净克隆：先 `git status --short` 确认本地工作，再执行：

```powershell
git switch main
git pull --ff-only origin main
git switch -c research/claude-subtitles-20261017
claude --model fable
```

若研究分支已经存在，切回它继续，不重建或重置。已有本地改动先保存，不能用reset/clean
清空。上述新分支命令在已有同名分支时会安全失败。

已安装并登录 Claude Code 即可先研究，无需安装/启动 AnimeService。
`fable` 是官方支持的选择方式，实际映射以 `/model` 显示为准，记录完整模型名与
Claude Code版本；需要固定5.1时选择账户可用的 `claude-fable-5-1`。
不自动换模型、购买额度或设置 API 密钥。仅想先看方案可选
`claude --model fable --permission-mode plan`；Plan模式不做源文件修改，进入实际实验时
应切回正常权限模式。默认方案使用正常模式，按下面提示词直接完成允许的研究。
[模型选择官方说明](https://code.claude.com/docs/en/model-config)；
[Plan模式](https://code.claude.com/docs/en/common-workflows#plan-before-editing)。

根目录 `CLAUDE.md` 提供简短项目指令；原机巨大的AGENTS/HANDOFF历史不入库。
不要一次加载全部文档、JSON和图片，先看基线，再按问题取证。
[CLAUDE.md官方说明](https://code.claude.com/docs/en/memory)。

## 可直接粘贴的提示词

```text
请作为AnimeService字幕制作研究者，实际分析并解决当前可验证的问题，交付可回带的方案、实验和代码补丁，不只列模型推荐。

先读CLAUDE.md、docs/PROJECT-RULES.md、docs/SUBTITLE-RESEARCH-BASELINE.md和docs/CLAUDE-SUBTITLE-RESEARCH.md，再按需读日文来源、两阶段制作、原声研究与遗留问题文档。先记录当前commit、分支、实际模型与工具能力。使用当前独立研究分支，不接管原机的生产任务。

把问题区分为材料缺失、候选已有但未匹配、声学证据不足、时轴/排版缺陷、流程/代码缺陷。优先解决已有日文候选的分段匹配、缺句和重复对白消歧，再研究歌词TV剪辑的句级计时、快速字卡/移动小字。魔王E2的C17/C254/C358只在出现新材料或实质新方法时做限定实验；复用已有失败记录，不轮换模型重复跑，不重翻已有全文。

先确认新电脑有哪些依赖和证据。仅有Git也要继续完成代码分析、官方资料核查、合成反例/回归测试及可复用工具改进；缺真实材料不能伪造音画测试。独立SubtitleSkillLab不在本Git仓库，不能把缺路径/模型当算法失败。

每次实际实验固定输入hash、实现/模型版本、一个主要变量、对照、时限、成功标准与退出条件。相同失败条件不重复；先完成一到两个最高价值的小实验，再按实际收益继续。明确区别文本匹配、ASR/OCR候选、实际审听、播放器技术检查和正式accepted。不能从中文反造日文，不能以歌词/文件名/v1标签证明普通对白完整双语，不能用第一次发声证明第二次。

允许在研究分支修改通用代码、补充有区分力的合成测试并执行隔离测试，修复可复现缺陷。原稿、已有成品、观看记录、生产DB、owner/lease/default/accepted、自动化和下载器保持；不启动或部署生产服务，不执行publish/begin/acquire等操作，不回修已看集。原声/视频/完整字幕和私有报告不自动上传到模型或第三方；只研究公开源码及脱敏摘要。确需私有数据时列明具体文件/范围和用途，请我决定。

把成果写入data/claude-research/<日期>/：REPORT.md、experiments.jsonl、必要的可复现脚本与原始结果索引。REPORT逐项写：问题ID、已知事实与根因、改变了什么、实测证据、收益/反例、未验证内容、原机如何沿唯一owner做不可变增量。保留完整可看v0→v1交付，疑难不再拖住整集。

完成后提供代码差异、测试结果、缺件清单、仍开放的问题及下一步。仅脱敏通用代码/合成测试可在研究分支本地提交；不要推送main、合并或发布。给我明确的回传命令和私有结果目录。
```

## 私有材料如何带到另一台电脑

本次原机另外生成 `data/research-handoff/20261017/subtitle-research-private-20261017.zip`。
它**不会随git pull出现**。需要真实证据时，由用户通过自己的文件传输方式拷贝。
里面有问题索引、直接引用的证据、Jimaku候选、有限原声审听材料、独立核心与技能源码快照；
没有生产数据库、凭据、观看记录、整集视频、模型权重或运行环境。

该包用于本地工具复现。Claude Code通常把读到的内容发送给模型；“文件在本地”不代表
“原声和私稿不会外传”。未经用户对具体数据范围授权，Claude只读公开源码与本仓库脱敏摘要，
不自动展开私稿、加载音视频、调用云转写，或将私有报告通过工具输出带入模型上下文。

先验证再解包；下面的 `<zip路径>` 由用户替换为实际位置：

```powershell
python scripts/verify_research_bundle.py '<zip路径>'
Expand-Archive -LiteralPath '<zip路径>' -DestinationPath 'data/research-import/20261017'
```

验证失败先停止，不覆盖已有目录。包内 `MANIFEST.json` 记录每个成员的SHA256、大小和来源
相对路径，`README-PRIVATE.txt` 解释目录映射。保留原始字节；原报告中的旧绝对路径是历史
来源，用清单映射到 `evidence/AnimeService/` 或 `lab/SubtitleSkillLab/`，不能直接执行旧命令。
清单只验证传输完整性，不证明内容正确、已审听或可执行。这是部分证据集，引用的深层素材
未必全部包含；缺件须列明，不能假装整套生产环境已搬迁。

## 隔离测试与依赖

需要跑Python测试时再创建本机环境：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q tests/test_research_bundle.py tests/test_subtitle_auto_enrollment.py tests/test_finished_viewing.py tests/test_bahamut_alignment.py
$testFiles = @(Get-ChildItem tests -Filter '*.test.cjs' | ForEach-Object { $_.FullName })
node --test @testFiles
```

macOS/Linux用 `.venv/bin/python`，Node测试用 `node --test tests/*.test.cjs`。
不运行安装自启脚本、`anime.run` 或 `deploy_*`，不连接原机4870/4871/11436。
若确需离线实验UI，只用临时DB、假媒体和明确关闭后台的独立实例。

完整 `pytest -q` 有部分字幕测试依赖独立 `SubtitleSkillLab`。研究包提供源码快照供定位问题，
并未自动安装到硬编码路径；需先解决路径/依赖或选择独立测试。FFmpeg/libass、字体、模型
与GPU也不随仓库迁移；缺项如实报告，不能为通过测试删门禁或跳过真实问题。

## 把结果带回原机

公开可用的通用代码/合成测试：检查 `git diff` 和暂存内容，在研究分支提交。
用户决定推送时用 `git push -u origin research/claude-subtitles-20261017`。
不想推送可将已提交差异导出为补丁文件：

```powershell
git format-patch -o ../claude-subtitles-patches origin/main..HEAD
```

未提交的改动不在format-patch里。私有实验/报告另存并私下传回，不加入公开提交。
原机先核新commit与当前代码的差异，重读逐集已看、原owner/租约及当前成品hash；
合并代码和采纳字幕内容分别验证。新材料沿原唯一owner新增不可变版本，不能覆盖旧字幕、
当前成品、观看进度或正式验收。
