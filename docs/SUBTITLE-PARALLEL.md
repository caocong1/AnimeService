# 多会话并行制作

## 2026-11-22 MacBook 母片抽帧与解码

本节适用于所有进入字幕制作队列的作品与季度。准备、v0 和 v1 共用项目级 MacBook 配置，各番按原队列和编码槽顺序自动接续。

在项目目录用 .venv 的 Python 运行以下命令。未领取的准备集按在追、未看、独立范围和绑定的已完成文件核验。已领取的制作集抽帧时附上本集实际的 `--owner THREAD --expected-input INPUT`。输出目录必须是 data/subtitle-studio 或 data/diagnostics 下的新目录。

```text
python scripts/probe_subtitle_decode.py SHOW EP
python scripts/probe_subtitle_frames.py SHOW EP --timestamps 126 694 1364 --width 0 --format png --output data/subtitle-studio/JOB/native
python scripts/probe_subtitle_frames.py SHOW EP --range 0 96 2 --width 1280 --output data/subtitle-studio/JOB/opening --owner THREAD --expected-input INPUT
```

时间单位为秒。定点模式逐个在母片上寻帧，范围模式只解码指定区间，按给定间隔输出分镜。Mac 用 VideoToolbox 解码，准备操作共用一个内核锁。图片按回传清单逐份验 SHA，完整解码检查音视频结束位置；源图经实际查看后才可作为洁净度或画面字证据。Windows 读取轻量日志、观看页及成片验收结果。当前配置下，Mac 失联会保留原工单供重试。

沿既有 SSH 配对和 render-env，将 scripts/mac_subtitle_media_worker.py 以完整文件 SHA 部署为 `~/.animeservice-worker/media-<sha>.py`，在 Mac 回读 SHA。Mac 的 source-cache 按完整 SHA 复用母片，第一次传输逐字节核验；同时到达的请求排队。准备、v0、v1 领取独立硬链接，缓存按最近使用回收，保留上限 48 GiB。新流程也可读取旧压制目录中长度和完整 SHA 均相同的文件。原制作聊天继续持有唯一票据；安装或登录 Mac Codex 本身不构成业务派发回执。

## 2026-11-21 失联恢复回执与实际制作容量

每轮用 `scripts/subtitle_recovery.py plan-round --before 本轮全新文件` 取得同一时点的可派集、输入、版本和容量。先逐集 begin、原生宿主派发一次、保留原返回、confirm、读回，再领下一集。三集实际制作、单槽编码和互斥发布继续执行。轮检分别呈现仍空的制作槽、未知回执和已确认派发。

停写的同负责人恢复可以经 `isolate-resume JOB --revision REV --native FILE` 核验，再加 `--apply`。须是原始系统恢复、已接受 interrupted 回执、过期租约、停滞超过一小时、五分钟内原生读取同一聊天最新终态、本机无对应写入进程并取得编码和发布锁。隔离绑定原任务、版本、输入和聊天，仅释放其它集的容量。原集保留回执和写入限制，实际迟到的 created 要重新核范围和容量。有效租约、新建未知及用户停止沿原保护。原生回读和本轮现场见 `data/subtitle-studio/delivery-runs/20261121-193255/closure.json`。

2026-10-22 用户要求多个会话同时处理积压，分别找片源、找字幕和制作。此规则覆盖旧的“整体只能一集主动生产”；每集唯一负责人、先 v0 后同 owner v1、中文防漏及全部范围保护不变。

## 容量与分工

通过 `scripts/subtitle_delivery_queue.py capacity --limit 3` 显式启用最多三集主动制作；缺省和非法配置仍为一集。`status.capacity` 是实时容量。不同集可以并行，同作品也可以；同集的 episode/refinement/late 工作项不能同时写，同一线程不能同时绑定两集。committing、unknown 和有效租约继续占容量，不因超时或调整上限清除。降低上限只阻止新领取，不停止既有负责人。

原调度按实时队列顺序，在可用槽内逐项 begin → create/resume → 原票据 confirm，每次动作后重新读状态。每轮最多补三个空槽，某集等待回执时继续按剩余容量派其它就绪集，不并行执行派发事务，不反复唤醒 active 线程。ready 集可以立即派发，不必等其它集缺稿问题解决；材料准备不占制作槽。

片源会话负责未到料集的实际来源、torrent、下载回执和本地文件核验；字幕会话负责已到料集的中日原稿搜集、正文检查、不可变归档和匹配候选。两类会话均不领取正文 owner，不写固定制作稿、不改 input。实际下载仍由原后台受控入口提交，不能另建 Engine 或下载器。已看、缺授权、未知归属或有效 owner 保护继续。

## 输出与共享文件

不同集语义、排版和核验可同时进行；整集编码使用跨进程单槽，等待时保留 owner 并定期输出真实等待状态。不能把编码排队记为素材失败、假 stopped 或已完成。发布使用共享索引锁和事务内重读，避免覆盖其它集新条目；不删除未知旧锁。启用并行前核旧编码进程，已在运行的旧进程不被杀死或绕过。

每集在独立私有目录保留证据、稿件及 RECOVERY.txt。共享源码、调度配置、原稿和统一问题档案不得由各制作会话并写；发现工程问题交调度汇总。2026-11-01新增`scripts/merge_subtitle_issues.py`安全追加入口：逐集文件绑定job/input/owner、实际尝试、下一条件和证据SHA；协调轮先核计划再加`--apply`，共享进程锁、冲突拒绝、不可变备份和原子替换保留历史。相同对象幂等保留，不覆盖旧问题；所有其它索引写入（含周复盘）也须持有`unresolved-issues/.write.lock`的`output_lock`。不能靠缺issue_id伪造v1通过；已归档须读回推进原owner收尾，不等每周复盘。原稿、媒体、旧成品、default、accepted、观看和停止的旧聊天保留。

## 编码进展与停滞边界（2026-11-08）

乱马637802/S03E06 v1 在仍有编码进展时被 `process.wait(timeout=1800)` 终止。`output_lock(timeout=5400)` 仅限制取得单槽的排队等待，不能视为编码运行租约，也不通过延长它修复误杀。

`finish_subtitle_episode.py` 现在通过 `subtitle_render_watchdog.py` 每5秒读取最多64KiB的 FFmpeg `-progress` 尾部，只接受以 `progress=continue/end` 完整结束的记录。真实编码帧数或输出时间戳超过已观测最大值才续等；持续推进没有固定总时限。启动后或上次推进后600秒没有进展即报 `no_encoded_progress`；第一次 `progress=end` 后另给600秒等待真实退出，重复 end 不重置期限。mtime、日志字节增长、速度变化、重复或回退计数不能续期。协议依据为 [FFmpeg 官方进度选项](https://ffmpeg.org/ffmpeg-all.html#Main-options)。

异常与停滞仅清理本次 `Popen` 所拥有的子进程，保存原始异常、停止是否确认、退出码及进度事实。停止未确认时保留 worker 标记，子进程继承的原 OS 单槽锁继续保护；不按 PID 重新查杀、不清未知锁。原片、失败日志和部分输出不删除或覆盖，同路径重试仍拒绝。只有实际成功退出后才进入既有解码、时长、音轨、字体、中文覆盖和发布门禁；进度 end 不等于成品成功。

本次只修工程依赖并隔离验证，没有恢复637802原owner、重编或发布。已有失败 v1 目录须保留；后续恢复仍由原调度核同owner、原input、停止/观看/范围、票据与预算，再由原owner选用新的不可变输出目录。证据 `data/subtitle-studio/delivery-runs/20261108-182330/maintenance-result/`。

## 持续接续

保留原 automation-2 的15分钟周期、目标聊天和 ACTIVE 状态，读取 `data/subtitle-studio/parallel-20261022/` 中片源、字幕分工的 RESULT.json。只吸收有真实文件/SHA/来源及当前范围核验的结果，按原受控入口补 readiness，不直接写生产库。分工线程 active 时只读跟踪；完成后若确有新任务可复用对应线程，结果未知先对账。原无进展守卫逐个检查合法制作 owner，同集或同线程冲突仍拒绝自动处理。

并行仅提高吞吐量，不减免中文字幕穷举覆盖、真实日文来源、六项 v1 工作或播放器检查。派发、找到候选与下载中均不等于成品交付。

## 2026-11-21　个人 Mac 转写与压制

服务和每集 owner 继续在 Windows 排队。正常转写走 SubtitleSkillLab 的 scripts/transcribe_media.py。配对机在该项目 private/asr-worker.json 指定 SSH alias 与模型名对应的已核模型 revision。只传提取的单声道 WAV，核音频 SHA 与模型 revision 后才落原候选稿。远端使用独立 ASR 锁。派发前离线时 Windows 低优先级至多两条推理线程。已发起的推理若失败保留错误。每次运行的 manifest 记录实际 worker。

整集沿用 scripts/finish_subtitle_episode.py 及原编码锁。主机部署文件 data/subtitle-render-worker.json 的 host 指向 SSH alias。Mac 对冻结视频、交付 ASS、字体验 SHA 后沿原 ffmpeg 参数压制。PTY 与远端 OS 锁约束编码进程。Mac 完整解码，Windows 在回传字节数和 SHA 对上后继续检查音频、字体、样帧与打包。Mac 离线时入口在创建输出目录前每 20 秒复查，最多等十分钟，期间恢复即可继续。同一份输入可在超时后重试。传回失败只重试拷贝，失败后 remote-render.json 留有具体 Mac 私有工作目录，可核收已有 MP4。

部署两份远端脚本时，以本机源文件的 SHA-256 小写作为文件名版本。Mac 的 ~/.animeservice-worker/venv 使用 Python 3.11、faster-whisper 1.2.1、ctranslate2 4.8.2，模型目录为 models/<revision>。将 SubtitleSkillLab 的 subtitle_lab/asr_inference.py 同步为 asr-<sha>.py。Mac 的 render-env 使用 Python 3.11 和 imageio-ffmpeg 0.8.1，其内建编码器具备 libass 和 libx264。将本项目 scripts/mac_render_worker.py 同步为 render-<sha>.py。两份机器配置在单次命令启动时生效。新部署未配置时仍使用本机。实测见 data/diagnostics/mac-subtitle-worker-20261121/RESULT.md。

## 2026-11-21 新下载位置与字幕交付耗时

AnimeService 在原机按 deployment.json 的 download_drive 决定新任务路径。普通集、首集样本与字幕样本写入 C:/MediaLibrary/Anime，原 qB 的新增任务单独指定 C:/MediaDownloads/Incomplete，先停写并回读路径再启动，迅雷新任务沿 C:/MediaDownloads/Incomplete/AnimeService 暂存。旧任务按各自台账回读，旧 D 盘原片和暂存链接继续识别。不要批改已有种子位置。原机同一 qB 的 708 个既有任务路径前后逐项一致。

622288/E8 v0 从首次到料到可播用时 48 小时 16 分 44 秒。首次就绪至负责人确认占 47 小时 18 分 52 秒，包含能力复验。沿原 15 分钟调度补槽，收尾对本轮新增的就绪集也复核实时空槽并继续派发。纯中文 v0 中有确切中文角色、仅使用指定 SC Medium 且无换字体/重置覆盖标签的显示事件时，按实际 ASS 校验用字。意外字体选择仍须验证。已成功编码的受控修复用单版本锁。回执与播放器检查见 data/diagnostics/c-downloads-fx-e8-20261121/RESULT.md。
