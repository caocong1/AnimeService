# 定时任务与持续制作

本文和automation-prompts保存审定规则与脱敏完整prompt；文档入库不等于运行任务已更新。周期/状态按本机配置核对，实际应用前须重新读配置、运行状态、原目标与项目，不能复建历史任务。时间按Asia/Shanghai，应用时核界面时区。

## 分工、周期与预算

| 任务 | 保留状态/周期 | 职责与本轮上限 |
| --- | --- | --- |
| automation：家庭影视库自动追更 | ACTIVE，每日12:30/21:30，原heartbeat | 独立家庭影视项目原五季范围；普通检查约10分钟，一集新可行动缺口；不是动画下载/字幕入口 |
| automation-2：字幕v0→v1连续制作与派发 | ACTIVE，每15分钟，原heartbeat | watching未看集材料/缺口只读诊断、唯一派发、真实owner跟踪；通常3–10分钟，10分钟后不开始新重活 |
| automation-3：想看首集实评与稳定更新 | ACTIVE，每小时，原heartbeat | wish/trial变化指纹、一个首集的可恢复真实评审批次；通常3–10分钟，未看完整集不得报完整评分 |
| automation-4：字幕遗留问题每周复盘 | ACTIVE，周日10时，原heartbeat | 仅有新条件的遗留单点，最多两项、单项10分钟、总30分钟；不是正常v1制作 |
| CodexDispatchGuard | 独立本机计划，每5分钟 | 两个调度heartbeat及当前有效唯一字幕owner的无进展诊断/精确中断；不解业务锁或重投 |

家庭影视任务只在其原项目运行、沿原下载优先级与五季授权；不得搬到AnimeService或套用动画的唯一下载器规则。需要桌面交互、文件移动/目录整理的步骤转独立维护，定时轮不开Computer Use、不启动下载器、不搬活动文件。四周期保留的理由是后台已负责发现/下载，协调不靠高频重试；v0→v1由持续owner推进，不等15分钟。

## 从下载缺口到字幕交付

1. 读AGENTS/PROJECT-RULES及最新短交接，复核selected、wish/trial/watching、原授权、映射/盘点、分集边界、cleanup_hold和逐集watches.finished；领取/继续/输出前都核，不能改个人或已看状态来入队。
2. 来源发现、download_gaps核对、ingest/下载与完成对账属于唯一现有Engine。定时任务只回读现有只读视图的分类与checked_at；reconcile_missing会写缓存/可能ingest，不是只读探针。无只读接口就登记缺口，不实例化Engine或直写SQL。
3. download_ready不等于下载完成，source_not_found不等于确证未发布；retry/主源窗口未到等待，candidate_review/quality_required/sources_disabled按真实原因处理。旧缓存标陈旧并核后台健康，不强制催抓、不绕下载回执/manifest/归属门禁。
4. 任务3只管wish/trial；任务2管watching字幕准备，每轮必须从status.preparation.available按最近实际尝试轮换一个作品，owner占槽也推进一次有界实际准备；规则见[准备积压处理](SUBTITLE-PREPARATION-BACKLOG.md)。不抢writer、不改固定input、不把重复读报告/refresh算准备。首集实评保存实际覆盖游标；无可靠断点能力就登记一次工程缺口，不用片段补整集评分或把定时轮无限延长。
5. 日文材料逐集核本地轨/存档、Jimaku、用户指定页，实际文件核作品/季集/对白/歌曲/来源声明并存私有URL/时间/字节/SHA及覆盖。查询失败、未发现、候选、待匹配、已匹配分别记录；CC标签仅声明，ASR人工稿非原生CC，同文ASS/SRT不是独立来源。跨平台按真实声画/剪辑锚点分段匹配，不凭片头偏移推整片。见SUBTITLE-JAPANESE-SOURCES.md。
6. v0可先交完整中文，普通对白日文缺口如实明示；finished、edition、日文覆盖、accepted四者分开。歌词双语、bilingual文件名或edition=v1不证明全双语，技术检查不等于亲听。
7. 唯一owner实际交付v0后，finished若返回continue_refinement=true，立即同writer继续v1；screen_text/op/ed/placement/effects/semantics六项须实际工作、证据与未决issue_ids，预算不授权整类略过。真正v1/文件/视频绑定/player核验完成才释放槽。publish经统一/watch入口，原视频/v0/v1并存，活动播放不强换，不回填观看/default/accepted。

## 原票据派发与回执

真实控制入口为scripts/subtitle_delivery_queue.py。status、receipts、reconcile-plan只读；refresh/begin/confirm/progress/finished/blocked/refine会写状态，不作为生产测试探针。

- 新派发先按实时revision做一次begin，CLI返回ticket_path；成功归档且原票据可读后才能调用宿主，严格按create/resume返回动作，保存真正负责threadId。票据内部create/reuse原值不改。归档后续失败可能已committing：立即停外部动作，不重复begin或从当前token补票据。
- 新confirm携原ticket，host_ref仅传真实宿主提供的关联，缺省允许；不能猜latest turn、把IPC requestId当持久operation id。真实桌面工具不返回准确线程回执时停止新派发，不能借legacy绕过保护。
- 回读receipt_id、receipt_accepted、state_changed、reason和resulting revision。created只说明聊天创建/复用，不是字幕finished/accepted。相同规范化回执重放返回首次结果；不换ID补身份。
- 丢响应使用interrupted --ticket，仅诊断、保留committing；failed同样不清owner/租约、不重投。v1无unknown结果；无原票据的既有legacy在途仍明确兼容，不补造历史v1，不开dispatch_receipts_required。
- 原票据跨revision/input/owner迟到时保留事实并读原receipts/reconcile-plan，plan当前不执行恢复；禁止用当前凭证确认旧回调。reuse失败不清旧owner，stopped/watched/scope变化继续保护。
- 第26轮临时DB/假宿主/真实CLI验收覆盖以上边界；不等于真实宿主端到端或终态API验收。细节以DISPATCH-RECEIPT-CONTRACT.md为准。

## 停滞、守卫与缺席诊断

acknowledged、notLoaded或旧成品存在不代表仍在制作；核精确turn、最新工具/输出、遗留进程和用户停止意图。interrupted/failed不证明工具已停止，更不证明无副作用。明确适用恢复授权且原writer真实停止才受控同owner恢复；已完成事故不可重放。保留用户停止、未知派发、committing、有效租约与其它writer。仅宿主明确isolated_owner_required且begin返回create时允许独立修订owner，不能唤醒旧停止正文聊天。详见SUBTITLE-STALL-RECOVERY.md。

保留桌面停滞修复：制作使用项目既有环境，实际字体测宽/ffmpeg/浏览器输出能力检查，不把Pillow等工具依赖失败冒充素材缺失。Windows票据ACL以.NET设置并读回，begin前预检；真正归档仍再校验，不降低权限标准，不事后猜造票据。

守卫仅对配置白名单目标、正确项目/heartbeat或精确登记恢复轮、inProgress、20分钟无日志进展且无运行工具同时成立时，按expectedTurnId请求中断；不释放业务锁、不创建AI轮次、不杀应用；2026-10-19授权增加当前有效单集owner的只读发现与精确turn中断，受控同owner恢复及重试上限见SUBTITLE-STALL-RECOVERY.md。脚本共享codex_dispatch.json及ANIMESERVICE_DISPATCH_CONFIG覆盖，wait_seconds为60–86400整数、非法回退1200；空targets/cwd或缺db_files拒启动，node先PATH。无--apply仅写诊断；业务调度不自行运行--apply或改白名单。

先看status.json新鲜度/整体error，再按当前调度聊天、精确turn及时间筛actions.jsonl的控制尝试和control_exit_code/receipt/error；每次有status，只有尝试控制才追加actions。不能把全局末行、缺行或IPC成功当业务完成，结合实时队列只续未完成动作，不重放整轮。

codex_absence_verifier是独立只读--dry-run采集器，不是生产核验器；报告固定diagnostic_only、absence_authorized=false。原ticket、真实可用host_ref、既有私有输出目录是前提，权限只核不修，报告/sha256独占创建。版本白名单state 1..61/history 1..8/logs 1..2，未知schema fail closed；默认20秒、200行，time-limit范围0.05..30、max-rows 1..1000；SQL预算不保证可中断OS文件I/O。错误/截断/超时非零，退出0仅声明诊断范围完成。没有宿主operation级终态API：投影终态/空列表/人工签核都不授权not_created、清owner或重投。详见DISPATCH-ABSENCE-EVIDENCE.md。

## 安静、质量与禁止事项

输入/问题指纹未变、未到窗口、活动owner、相同旧故障或同SHA材料保持安静；昂贵hash/解码只随真实新身份或问题变化，必要的完整SHA不能省略。记录真实动作/证据、下一触发与通知指纹，使用既有受控记录，不另造生产表。仅新合格首集/版本交付、新实质失败或需用户动作去重通知；不无限期冒称被阻塞owner正在工作。

周复盘只处理新条件遗留单点，不代替日常v1；无问题安静，不自行删除任务。仅既有授权且隔离的能力试验，新环境/长下载单列owner方案。已看不自动生产/回修；制作中变已看安全保存并停止。定时轮不写逐句正文、不唤醒停止线程，不启动/切换下载器或第二Engine、不服务重启/Computer Use、不付费或外传音视频、不手工改DB/owner/lease/观看/任务提示词，不常驻等待。

## 模板、应用与证据

automation-prompts四份txt/manifest是完整脱敏导出；项目/目标保留现值的文字是说明，不能写进字段。日期性修订已并入规则正文，具体事件仍留历史交接；本轮不改SUBTITLE-SESSION-PROMPT.txt，完整保留桌面10-18停滞/依赖/唯一owner修复。

源配置SHA和原prompt摘要应用前逐项核对，确认没有正在执行的协调轮；用桌面原生任务工具只替换prompt并保留rrule/ACTIVE/kind/target/project及所有其它字段，独立读回规范化SHA/字段后再下一项。原生工具不可用、源变化或在途则停止；不猜IPC、不写运行TOML、不Run now。失败只恢复本次修改且无并发新编辑的那项并读回，禁止批量恢复或重建。CLI缺管理界面不影响它用于隔离验证业务调用协议。[OpenAI官方定时任务说明](https://learn.chatgpt.com/docs/automations)。

dispatch强制开关、播放v2宣传与迁表保持现状；文档/测试提交无需worker替换。真实配置应用及自然计划轮观察另有脱敏回执，不能因文档或磁盘文件更新就宣称应用内存已生效。
