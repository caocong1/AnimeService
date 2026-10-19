2026-10-15 用户最新更正：执行 [每集v0→v1双版本流程](SUBTITLE-TWO-PASS.md)。先上架v0观看版，紧接着由同一负责人完成v1精校细调；画面字、主动查找并制作OP/ED歌词、位置避让、必要动效及改写语义/时轴回查是每集v1必做项，不能为快整项省略。具体无解项留实际尝试和证据后归档，不卡几天。已看集保持原字幕，不自动回修。v0交付后不释放唯一writer，v1处理及输出完成才结束；保留两个观看入口。此条覆盖下文旧“首版即停止、只等新材料修订”安排，旧内容留作历史。

# 字幕库：观看交付与实际制作进度（2026-10-14）

用户明确要求由系统按新流程设计，而非沿用材料档案标签。页面主对象是“本集观看交付”，主列表仅有效在追范围的未看未交付集，以及真实已上架成品。其它个人状态和已看旧工作稿留在折叠历史档案，不删除、不重新授权。

状态语义：待开始（还缺素材，或齐料排队未派发）；准备中（有真实材料或下载任务，细分素材下载、回执核查、视频已到待盘点字幕、原稿已收集待视频、匹配本集、候选画面/输出通道检查）；制作中（已持久派发唯一负责人，真实阶段为整集文字校订、排版画面、整集输出、解码播放检查、上架）；已做好（finished观看成品或真实当前accepted版）。暂停/未知派发不伪装活跃阶段，有原因和具体下一步。缺JA与全面亲听只作首版后的修订记录。

每集显示首版或修订版。后续late差分工作项与当前成品并列，修订制作期间仍可播放旧版。未开始、材料准备和工作稿存在不是同一状态；仅有workbook不能据此显示制作中。每个等待/制作条目说明缺件及下一动作，计数不制造预估百分比。

素材资源按当前同集实际task manifest/文件尺寸核完成、下载中、待提交/核查，跨hash去重并过滤黑名单/保真：原片候选（明确生肉/可关软字幕标注）与熟肉参考分开，标签不足显示类型待核。候选数量不证明全片洁净或正式冻结；真实中文字幕档案另列，简繁/同文不凑独立译者。

`anime/subtitle_delivery_view.py`生成不含路径、令牌、owner ID的公开投影。CLI refresh/控制写入后更新static/subtitle-delivery-status.json；页面每分钟/重新显示时GET刷新，不重启现有服务。该文件按现有本机静态访问保护提供，Git忽略。负责人用`progress JOB --thread-id THREAD --stage writing|layout|rendering|checking|publishing [--done N --total M]`如实更新；当前确认owner、输入和生产状态守卫，未知/异owner不能改。输出配置可附delivery_job_id/delivery_thread_id自动回报encoding和checking。报阶段不改正文/default/观看/下载。

进度失联/过期显示提示，不把档案推测成新生产；成品索引独立读取，制作视图不可用时仍可播放旧成品。筛选/展开保持，全部操作为只读浏览。验证：.design/subtitle-library/delivery-first/contract.md、verification.json；Python临时DB/FakeQbit与JS模型、实际headless桌面/390px三主题深浅均覆盖，不使用Computer Use。

---

# 字幕库与观看来源

用户2026-10-11要求独立字幕菜单、按作品查看、播放页能看到实际文件和字幕来源，最终使用系统挑选的生肉加系统制作的字幕，熟肉仅用于字幕分析。

入口 `/subtitle-library`，API `/api/subtitle-library`。每集显示书面校订进度、整集是否可观看、当前视频、自制字幕文件/版本和历史生成版本；作品、状态筛选及搜索。历史研究记录保留，不因弃番或暂搁重新取得研究授权。生成或书面校订完成不自动成为观看版本；下载ASS仅提供当前视频精确绑定且通过既有 authored 门禁的版本，文件读取时重新核验。

播放页播放器下方“当前播放文件与字幕”，完整文件名和当前轨来源。字幕API明确 origin=embedded/authored、字幕文件名和自制build_id，不返回私有绝对路径。来源说明随实际渲染器的加载中/使用中/失败/关闭更新，修订号保证旧响应不覆盖当前切换。即使只有一轨也可关闭；没有软字幕轨不推断无画面硬字。

制作目标与当前视频指纹不符时单独说明需重新核对时间轴。既有整集语义、日语/声画、片内文字、实帧/字体及输出hash、来源黑名单与保真门禁继续；本轮没有改验收或自动激活逻辑。

最终观看策略为合格原片画面（生肉或已核实可关闭软字幕的视频）＋自制中日字幕。当前已有视频和工作稿处于过渡；熟肉作为逐集提取/比较基稿的研究输入。真实字幕通过所有质量证据后可按原授权激活，无需再询问；新生肉仍须完整作品季集、选集、授权、任务归属/完成证据和精确媒体绑定，本轮不替换旧视频、不新增实际提交。

实现 `anime/subtitle_studio.py` 的只读 library catalogue、`anime/webplayer.py` 来源字段，`static/subtitle-library.*` 和播放来源区；隔离验证 `tests/test_subtitle_library.py`、`.design/subtitle-library/verify.cjs`，设计/验收记录 `.design/subtitle-library/`。

2026-10-13随包外挂修复：播放器支持当前分集已完成本项目任务、原torrent hash及选中文件清单精确绑定的同视频外挂ASS/SSA/SRT。只读取归属目录内、同目录同视频文件名前缀、大小匹配的选中字幕；不能任意扫描目录或套用其它集。轨道来源显示“发布包外挂字幕”，AI翻译标注保持，中文优先默认；这不是系统自制或整集校订验收。字幕内容hash参与提取缓存，服务启动后生效。装备657656/E1、E2随包SC原件位于qB临时目录，两片视频与归库视频为samefile，两字幕所在原torrent分块SHA1核实后建立NTFS别名；不改媒体、任务、下载器或观看。私有证据data/subtitle-playback-repair-20261013/，空闲单次加载助手scripts/finish_sidecar_playback_reload.py会避开活动播放，deployment.json回读才确认网页已加载。


## 2026-10-15 成品在追番与分集页直接观看

成品上架后，追番首页、全部追番和作品分集页也必须读取 `static/finished-media/index.json`，不能只凭 `episodes` 下载表把已交付集显示为缺集。`FinishedReleases.applyBoard/applyList`按作品ID、当前季号及本地集号合并只读观看投影，保留已看，更新可播放/待看/下一集；v1精校细调优先，v0首版可单独选择。入口统一使用普通追番放映室 /watch?release=不可变ID，不回填或覆盖原视频/下载/default/正式accepted。

页面读取清单后用最多4路并发HEAD核验MP4存在、非空、类型正确；索引与核验共用8秒上限。失效版本不能新增播放按钮；v1失效且v0实存时可播放v0。独立成品不继承原片的续播位置。已有页面刷新加载，新成品在下一次读取时自动出现，无需重启服务。失败读取提示并保留原下载观看入口。

验证：`node --test tests/finished-releases.test.cjs tests/airing-schedule.test.cjs tests/following-pagination.test.cjs`；真实只读浏览器检查 `scripts/verify_finished_viewing.cjs`，证据 `data/finished-viewing-repair-20261015/`。


## 2026-10-15 后续每集上架与入口核验（用户明确要求）

每一集v0/v1都必须通过`publish_finished_episode.py`登记统一成品清单，自动接入字幕库、追番首页/全部追番和作品分集播放入口。仅编码完成或只挂字幕库不能报告观看交付完成。单集负责人须只读核本集首页/分集状态不再错误缺集、按钮实际可打开；v1优先且v0保留，不能伪造accepted或修改原片/default/已看来凑状态。

已打开观看列表在可见时每60秒及重新显示/聚焦时自动检查清单与文件，仅有变化才更新观看区域；分集页未保存表单保持，活动播放器不切换。新页面直接读取最新成品，无需服务重启或逐作品手工配置。暂时网络/5xx故障保留当前显示并重试，文件实际404则不能新增播放入口。具体实现与核验见`static/finished-releases.js`、`scripts/verify_finished_auto_delivery.cjs`与`data/finished-viewing-repair-20261015/auto-delivery-proof.json`。


## 成品播放器的观看记录

成品使用 `/api/finished-viewing` 读取记录；`/{release}/progress` 保存到共用 `web_progress`，键为 `finished:` 加 release ID 与视频 SHA 的摘要，普通原片及其他版本互不串位。`finished_playback_receipts` 以浏览器会话序号拒绝延迟/重复请求覆盖最终位置。实际播放后的每15秒、暂停、隐藏或离开保存；仅打开或未播放的拖动不写。网络/会话错误明确提示，不能假报同步成功。

`/{release}/watch` 校验当前作品季集后复用原 `/api/shows/{id}/watch`，因此已看、计数和追完边界与普通播放器一致；各版本共用该集已看。播放结束提示手动确认，跳到片尾不标已看。看完后重播从头开始，取消已看后可继续本版本原进度。`/{release}/viewing-session` 使用原播放租约保护，同一作品/本地集号阻止活动播放中更换原目标。

首页和分集每次刷新/返回、每分钟检查时一并读取服务端记录；有可续播的成品时保留最近实际观看的版本，v1仍可另选。无续播时v1优先。版本归属已变化或视频已缺失时拒绝记录写入。首次上线前未保存的旧进度无法追补，不借用原片/其他版本时间。可复用 `tests/test_finished_viewing.py` 和 `scripts/verify_finished_tracking.cjs` 验证；后者仅对隔离4879临时DB写入，生产媒体只读。


2026-10-16用户最新更正：自制字幕只是播放源，统一使用普通追番 /watch?release=不可变ID，同一Artplayer、弹幕与分集界面；不得另建独立播放器。原视频/v0/v1在播放源选择器切换，旧finished-player链接仅兼容跳转。版本续播仍走/api/finished-viewing，已看共用原watches；不强换活动播放。新交付回执和首页/全部追番/分集/字幕库必须使用统一入口。隔离浏览器验证 scripts/verify_unified_player.cjs 配合 scripts/unified_player_fixture.py；真实观看记录禁止用于测试。


## 2026-10-20 字幕队列：按制作顺序看每集在等什么

用户反馈多部在追作品长期没有字幕、一集卡住后面全部跟着等，而字幕库只按"已做好/制作中/准备中/待开始"分类，看不出顺序、位置和是否卡住。新增只读页面 `/subtitle-queue`（主导航"字幕"入口；字幕库保留为"成品与历史"）与 `GET /api/subtitle-queue`（`?show=` 只筛选条目，位置、制作槽与汇总保持全局）。作品分集页在每个未看集下方多一行"字幕 · …"，并在排队或疑似卡住时链接到队列。

`anime/subtitle_queue.py` 只读 `subtitle_delivery_jobs/events`、负责人阶段回报、finished 观察、`episodes`、`download_gaps` 缓存与 `queue_snapshot`，不实例化 Engine、不写任何表；投影不含负责线程、租约/派发 token 或本机路径，负责人写的阻塞原因会把路径替换成"[路径]"。每集状态只取下列之一：`producing`（已确认负责人；阶段与最近回报）、`starting`（committing/unknown 启动回执待确认）、`queued`（就绪，按当前队列快照顺序给出第几位与前面几集）、`preparing`（素材/洁净/能力门未齐，给出原因码）、`downloading`、`waiting_video`（给出 download_gaps 的原因与重试时间）、`blocked`（含 blocked-stopped）、`v0_ready`（已交付 v0；附 v1 后续的同样状态）、`done`（v1 已交付）。

"可能卡住"只由记录的活动推断：制作中超过 90 分钟、启动回执超过 30 分钟没有任务变更、事件或阶段回报就标 `stalled` 并给出时长；它提示去核桌面会话，不证明会话已停止，也不改变任何调度状态。页面同时显示制作槽占用情况与调度守卫 `status.json` 的最近检查/活动时间（不含线程 ID）。验证：`tests/test_subtitle_queue.py`、`tests/subtitle-queue-model.test.cjs`。
