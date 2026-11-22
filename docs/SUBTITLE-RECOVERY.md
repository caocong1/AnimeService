# 2026-11-22 有空槽时按固定母片完成逐集准备

1. 原协调轮读取 `plan-round` 的实际空槽、可派集与逐集门禁。对在追、未看、已正式上线、已有独立授权且无负责人或待确认派发的集，从已对账文件清单核季、集、片长及文件，再用 `bind-target --dry-run` 和原入口登记。轮内的只读数据库快照在写入后重新打开，再取当前 input 执行 `prepare_chinese_source.py collect`。
2. 复用同一母片已归档的中文正文轨，保留简繁、来源和 SHA。取到其它版本的日文 CC 时，保存原文件与出处，按开头、中段、结尾的对应对白评估各版本偏差，把实际取舍交给负责人。中文漏译、说话人、听核、显示行数及逐段变轴继续由制作和发布入口核验。
3. 在中文对白出现处从首中末抽取至少三张关闭软字幕的实际源帧。使用 `scripts/probe_subtitle_frames.py SHOW EP --timestamps ... --width 0 --format png --output 私有新目录`，实看后把图与 SHA 登记为 `base_review` 的 `sample_only`。片名、演职员表及片尾自带歌词逐项记录避让位置。运行 `scripts/probe_subtitle_decode.py SHOW EP`，按固定母片保存全片音视频解码、片长、SHA 和进度。在已配对的主机上，两项准备共用 Mac 的解码槽与母片缓存。重入时先读同母片的新鲜解码回执，Mac 离线按原工单等待。详见 docs/SUBTITLE-PARALLEL.md。
4. 同一母片另做带中日字体的短样编码并用本地播放器实播、暂停、跳转、播完。以真实结果经 `register-evidence --kind delivery_capability_report` 先试算再登记。原协调轮重读 ready 与实时容量，按现有票据完成发送和确认；每集保留唯一 owner。准备样片留在私有证据目录，可播交付仍须通过 `/watch?release=` 入口验收。

# 2026-11-07 独立准备路线不能被主门禁等待掩盖

`recovery_plan.next_action` 从主路线与 `independent_routes` 一起选取真实可行动项。主输出/洁净门禁正在冷却或已受阻时，未耗尽的独立中文准备仍进入原协调轮及五分钟无进展监控；已超时动作优先，同等期限内继续优先输出核验。保持原 job/input、各路线 condition、失败冷却与预算，不以切换 action/condition 重置同一 job/input 的监控时间。

`round-check` 除核原有新鲜度及匹配证据，还要求轮后 `checked_at` 不早于轮前；旧等待快照不能关闭本轮待办。同 job 仍有其它到期动作时，单条 `progress` 回执及路线消失仍采用保守判定，不据此自动宣称整轮完成，须保留具体 pending 和实际门禁后置证据。以上均为只读规划/审计修复，不新增派发或恢复权限。复现与回归见 `data/diagnostics/project-iteration-20261107/recovery-audit/`。

# 2026-11-01 正式先行配信与电视资料日期分开

`airings.airdate`保留资料站的电视播出日期。官网已明确先行配信且本集身份经实帧核验时，可登记独立的`verified_early_release_作品_本地集号`事实：version=1、show_id/season/episode、映射后的source_episode、原catalog_airdate、带时区的released_at、官方source_url以及data内证据path/SHA256。原日期、季集映射或证据SHA变化后原事实失效，须重新核验；不以文件到位、标题或发布组标签单独推断已正式上线。

`viewing_workflow.unreleased_date`统一用于字幕队列、refresh、准备/恢复、reserve/begin；经核实的先行版到正式上线时刻后才退出unreleased，未来先行时间仍等待。此事实不授予下载/制作范围、不制造ready/accepted，不解除已看、停止、原owner、unknown/committing和租约保护。542007/E6已核官网提前一周配信、片内第6话标题及第7话预告；私有证据`data/diagnostics/daiseijo-e6-release-check-20261101/`。

用户随后要求扩查全部到料冲突、以后主动处理。`anime/official_releases.py`增加有界的官网周更排期，不无依据整季平移：绑定show_id/original/season、完整mapping_sha256、episode_start/end、catalog_anchor的本地/累计集号与原电视日期、interval_days、带时区stream_anchor_at、官网source_url、真实verified_at/valid_until及data内原页面path/SHA。首日双集以第2集为锚点；JST的24:00必须转换为次日00:00，再换算北京时间。每个后续目录日期和source_episode都必须精确吻合，否则该集回到原日期并进入核查。

排期有效期运行时最多8天，正常登记7天；到期前24小时进入`revalidate_official_schedule`，原15分钟任务须实际重查官网延期/平台信息并保存新证据，不能只延长时间。页面读取无联网/ffprobe/全片哈希；只读事务内复用同一排期核验，下一请求与reserve/begin重新核。全盘媒体日期核查只在调度/恢复快照运行，用户字幕队列不运行未使用的全盘诊断，但仍逐集核正式上线门禁。正常refresh仅将已到正式时刻、尚未看且目录/映射未变的事实幂等保存，保留既有事实不覆盖；过期后已确认上线的历史事实继续有效，尚未上线的集不能沿用过期排期。

```powershell
./.venv/Scripts/python.exe -X utf8 scripts/official_releases.py plan
./.venv/Scripts/python.exe -X utf8 scripts/official_releases.py register-schedule RECORD.json
./.venv/Scripts/python.exe -X utf8 scripts/official_releases.py register-schedule RECORD.json --apply
# 替换时必须加 --expected-sha 计划中的old_sha256；旧排期按SHA保留不可变历史
./.venv/Scripts/python.exe -X utf8 scripts/official_releases.py record-review REVIEW.json
./.venv/Scripts/python.exe -X utf8 scripts/official_releases.py record-review REVIEW.json --apply
```

`plan`跨episodes完成文件、已存字幕母片与完成/linked下载任务清单核实际文件和大小，即使没有字幕job也会发现冲突。待核清单放入`subtitle_recovery.py status`的`official_release_review`；原automation-2每轮先检查，最多实查一个新官网/排期，再继续原制作工作。新作品核完整时长、片内集号/集名、头中尾，排除PV/剪辑/错集；已核周更规则用于后续集的正式上线时间，文件身份仍由原材料和制作门禁复核。无完成视频的真实未播出不误列成制作失败。试看/想看允许元数据纠正，不改变用户状态、授权或自动进入在追制作。

`record-review`接受show_id/episode、plan.condition、outcome（source_unavailable/not_formal/awaiting_official_release）和实际evidence.path/SHA；相同回执幂等、旧记录按SHA归档，事务拒绝并发覆盖。相同条件首次冷却30分钟，后续重查至少隔6小时；新文件身份/映射/目录产生新条件。已知未来官网时间直接标awaiting_official_release，不每轮重查。不能以旧失败或只刷新永久跳过，也不要求用户逐集提醒。此台账独立于原字幕准备路线失败预算，不清原失败或恢复次数。

2026-11-01全盘实际仅另发现456078/E6：官网dAnime/ABEMA提前一周、首日第1/2集同发；第6集正式上线为11月1日00:00 JST＝10月31日23:00北京时间，片内《風雲!! 魚布林城》与目录吻合，全23:40。当前试看状态保留；362551/E1及616808/E6均无完成视频继续等待。两部已核排期经受控入口登记，原automation-2已原生更新及全文回读，15分钟/ACTIVE/目标与其它任务SHA保持；证据`data/diagnostics/official-release-automation-20261101/`。

# 2026-11-01 需要留意必须进入实修闭环

用户再次明确要求主动解决字幕问题，不依赖每次手动催办。原automation-2通过应用工具实际应用新流程并回读，仍原聊天/15分钟/ACTIVE；不再将只更新模板称为已部署。业务恢复及原五分钟Windows守卫统一使用项目`.venv/Scripts/python[w].exe`。系统Python经洁净下载投影导入OpenCC会失败，不能沿用旧健康结果。

已停写原owner的`repair_blocked`现在产生`repair_owner_dependency`，进入恢复候选及六小时/30分钟监控。此动作仅要求协调轮实修依赖，不授予宿主恢复，不改变stopped/owner/input或解除用户停止；真实失败仍冷却30分钟、最多两次。还须核页面stalled、原RESULT及v0后未完成v1，不以空action永久跳过。守卫`already_attempted`保留`control_outcome`、原结果和`reconciliation_required`；no-client-found、失败或未知只沿原生精确终态/末工具/遗留writer对账，不重复同turn控制、不吞成健康。

问题档案由`scripts/merge_subtitle_issues.py PENDING --job JOB --owner ORIGINAL --expected-input FIXED`核计划，`--apply`才幂等追加；验证逐集身份及证据SHA，同ID冲突拒绝，共享进程锁和不可变备份保留历史。相同问题已归档即回读推进，不等旧文案或周复盘。归档不代表问题已解决、允许恢复或v1交付。工程依赖实际关闭后保存新证据，重核原生终态/工具/无writer、原owner/input、范围/未看；据新真实系统诊断沿原blocked入口登记`system + 原精确turn`，再按原自动预算与票据恢复，禁止仅改标签绕台账。过期能力仍须真实复验并经同owner入口补证。

证据：`data/subtitle-studio/attention-repair-20261101/`。恢复、补证和新工具不等于成品交付。

# 2026-10-24 恢复闭环、证据台账与防空转

本轮用户要求解决以后反复暂停，而不只是截图中的单集。现行直接原因包括：守护任务用系统Python运行，只读中文准备链却经 `subtitle_studio` 导入 FastAPI，导致准备监控连续失败；以及桌面turn多次completed但没有真实 `preparation_attempt`。`anime/subtitle_lab.py` 现在独立加载字幕核心，网页模块只 re-export；只读恢复监控不再依赖网页栈。`update_monitor` 对快照失败也写新鲜 error，不能沿用旧健康文件。

## 完成一轮的判定

2026-11-04新增只读`subtitle_recovery.py round-check --before 本轮recovery-before.json`。轮前快照必须本轮真实status且一小时内；有到期准备/独立路线/官网复验或空槽ready，且没有匹配身份的新真实失败/受阻或门禁变化时退出2。只刷新时间、换condition/action及未变门禁的progress不通过。一次有界真实处理可收束但仍须列pending。原helper已做动作而漏入台账时，核当前input/路线/来源修订及原证据SHA后经record幂等补账，明确旧动作发生时间，不重复执行或冒称新检索。见[整体契约](PROJECT-CONSISTENCY.md)。

- 原 `automation-2` 保持原目标、原聊天、15分钟和 ACTIVE，不新建重复任务。桌面turn completed只说明聊天结束；有效协调轮必须留下真实动作、私有结果证据和动作后的状态回读。只读status/refresh、复述旧报告或不执行任何路线不算实修。
- 原五分钟守卫的 `data/diagnostics/subtitle-recovery/status.json` 增加 `execution`。同一job/input没有新的失败/受阻或真实状态变化，持续30分钟即 `execution.status=stalled`；只换动作名称、condition或refresh不能重置计时。仅登记 `progress` 而 condition/status 不变不能重置该计时。页面健康栏分别显示监控异常、记录过时和超过30分钟无实际动作。
- 未完成 v0/v1 refinement 也进入恢复规划；`recovery.domain=owner_recovery` 的超时显示“制作恢复超时”，不再冒充素材准备。已有v0但能力/baseline失效时仍须受控补证，不能仅凭此规划变化伪造ready。
- 无媒体盘点时间时，以job创建时间作为稳定的提醒上限，不用每轮now推迟六小时；refresh/revision及阻塞文案排序/重复不重置条件。范围授权保护、`source_wait` 与真正可处理的owner故障分开展示，范围保护不自动扩权也不阻挡其它合规集。

## 结构化停止与自动恢复计次

工程停写由负责人/调度在核新鲜宿主终态、最后工具、遗留writer、未看、范围和固定input后记录：

```powershell
./.venv/Scripts/python.exe scripts/subtitle_delivery_queue.py blocked JOB --thread-id ORIGINAL --stopped --reason "具体系统故障及证据" --recovery-class system --recovery-turn ORIGINAL_TURN
```

材料不足用 `--recovery-class source_wait`，工程入口受阻用 `repair_blocked`；明确用户停止用 `--user-stopped`。无结构旧记录及含义不明记录保持保护，不能根据自由文本推断自动恢复授权；旧11436文案本身也不再生成 `recover_owner`。所有 `blocked-stopped` 的无参数 `begin/resume` 一律拒绝：自动路径必须带原turn，人工路径必须显式 `--manual-recovery`，不能借 `protected` 分类绕过计次。`owner_stopped=false` 仅允许当前acknowledged的活动owner；这种 active-blocked 仍由原owner guard监控，不脱离守卫。

人工恢复与自动恢复都只保留 pending 状态到原created回执；回执成功才清除停止事实，失败/释放未派发租约必须保留原 `owner_stopped_writing/user_stopped` 和分类。首次refinement的合法pending payload与迟到created对账也按原ticket处理，明确用户停止不放行。

原票据自动恢复命令为：

```powershell
./.venv/Scripts/python.exe scripts/subtitle_delivery_queue.py begin JOB --owner DISPATCHER --revision CURRENT --auto-recovery-turn ORIGINAL_TURN
```

服务在reserve先核结构化system停止、原turn、范围、未看、容量与当前台账；CLI在原票据归档并用 `verified_ticket` 校验后，`mark_auto_recovery` 再在写事务重核dispatch/范围/已看/台账并写入 `owner-auto-recovery`，然后才把控制交还宿主调用方。该点之后宿主超时或结果未知也已消耗一次，不能重发。按事件自己的 `owner_thread_id + turn_id` 去重，首次不可回退时间决定24小时窗口；同owner最多两次。全历史同turn禁止重放，24小时预算窗口不会遗忘旧turn。手动明确恢复不走自动参数，不增加也不清空自动台账；所有stopped恢复都必须显式选择自动或人工模式。第三次及后续空挂仍由守卫精确中断，核停写后让槽；限次只限制resume，不限制防卡中断。

可信非模型create/resume桥尚未部署；本功能不把普通后台变成宿主，也不允许绕过 `subtitle_dispatch_once`、原票据或unknown/committing保护。自动压缩的完成确认仍是独立前置：compact结果未知、仍有writer或宿主终态不清时不得发送恢复，后续须由原生桥提供可验证的compact-then-resume，而不是把完整旧prompt反复塞回长聊天。

## 台账不可变与幂等

`record_attempt` 先保存证据内容寻址私有副本，再按 condition/action/排序后的内容SHA集合去重；同bytes跨路径或引用换序不重复计数，同路径改写不会破坏旧证据。attempt只存小型绑定字段，不写整份candidate/source_preparation，避免单次大记录毒化整个状态读取。冷却或耗尽后拒绝新增同条件failed/blocked；字节相同的回执即使路线已前进也可幂等重放。成功但无后置状态变化不算完成一轮。

# 2026-10-23 暂停任务获新请求后的同owner恢复

用户明确要求恢复截图暂停字幕并行制作。本轮证据在 `data/subtitle-studio/resume-paused-20261023/`。最多三集，保留原owner/input、自动失败计数、未知回执和已看保护。本次手动恢复不计作自动恢复上限清零，也不允许无界重放。

`register-evidence --thread-id ORIGINAL --expected-input FIXED` 扩展到未交v0的 `episode/blocked/blocked-stopped` 原负责人：必须 `owner_stopped_writing=true`、范围有效未看、无有效租约、当前母片与固定assessment一致；若存在v0或finished观察，仍须完整相同baseline。原参数缺省的无owner保护保持。报告通过真实验证和事务重核后只保存新证据历史及最新评估，保留最初assessment、固定input、owner、停止状态和旧失败。

原owner制作中追加日文参考可能改变实时原稿集合。派发时只在原completed_media完全相同、每个原archive条目及签名字段全保留、原中文hash仍存在、新旧input均可由真实档案重构且确有新增档案时，重放固定输入。移除/改写原稿、目标变更或未知签名漂移继续拒绝；这不是换母片入口。恢复仍须新鲜宿主终态/工具/外部writer证据以及begin原ticket核验、只恢复同聊天并confirm。

2026-10-22 无人工制作：执行 [中文字幕来源与无人工作业](SUBTITLE-CHINESE-SOURCES.md)。Codex 自动检索并核对同集熟肉软字幕/硬字幕 OCR；已有全片 OCR 直接校对与跨版对轴，不能因独立稿搜索失败而停止。字幕缺稿、待转写或待校对不转人工/用户介入；来源候选与实际可用主稿、ready/finished/accepted 分开，中文防漏及唯一 owner 等保护不变。本条覆盖下文旧人工前置。

# 字幕准备的自动修复与介入

## 2026-10-23 用户指定四集恢复

用户要求恢复字幕库截图中的暂停项：558064/E7、E8、E9及624085/E3，继续原负责人v1，不覆盖现有可看版。本次明确恢复请求可处理原系统失败恢复次数已满的项目，但不删除历史次数或授权无限自动重试；最多三集主动制作，其余按容量候补。

`register-evidence` 增加显式 `--thread-id` 与 `--expected-input` 成对入口，只允许未看、范围有效的 episode/blocked/blocked-stopped、已停写原owner、无有效租约、refinement_pending且v0回执与当前成品观察一致。当前母片fingerprint及重新评估input必须保持。报告仍按原规则验证文件、SHA、媒体、有效期和真实检查；事务内重核全部范围/任务/原稿/目标/观看事实。只更新证据及assessment，保留blocked-stopped、owner、input、v0/派发历史；恢复另走原begin→同聊天→原ticket confirm，不把登记视为开工或交付。无参数的准备入口保护不变。

## 2026-10-22 本集负责人获明确授权后的母片纠错

558064/E8 的完整 WEB 原文件仍在本机，旧媒体库链接缺失后准备流程曾绑定含前后节目/宣传的 AT-X TS。用户明确同意补充同负责人及旧 input 校验的受控纠错，再继续 v0→v1。

`bind-target SHOW EP --path COMPLETED_FILE --thread-id CONFIRMED_OWNER --expected-input CURRENT_INPUT [--dry-run]` 仅用于已经明确授权的母片纠错：未冻结、未看、未交付、kind=episode、production/acknowledged、同一负责人、精确当前 input、无有效租约。仍须本集已对账完成任务的选中文件及尺寸/来源保护；事务内重核全部事实。无两个新参数时原无 owner 门禁不变。不得用此入口接管、恢复用户停止、回修已看或更换已交付基线。

成功仅更新临时制作母片/assessment/input，保留 production、原 thread、派发票据/回执、租约历史、accepted、原视频和观看；`owner-target-corrected` 事件及 payload.target_corrections 留原目标、原 input、原 assessment。旧目标的洁净/输出证据不被视为新目标通过，负责人仍须按新目标实核。旧 input 重试拒绝；回读新 input 后同目标重放幂等。没有重启服务、下载器或新建 Engine。

2026-10-21 用户要求：排查流程、自动修复重复故障，确实解决不了才通知用户介入。本条覆盖旧调度中“无新条件一直等待”、禁止修复本地已登记审听服务的笼统限制，保留范围、已看、唯一 writer、未知提交与原版保护。

根因：准备过程只有散落的 closure 记录，没有按具体输入持久累计尝试；准备时间不纳入停滞守卫；未冻结候选的受控绑定被误认为禁止改变固定输入；本地临时审听进程退出后无人恢复。状态刷新、等待材料、真实修复和制作交付必须分开。

## 原有两条执行链

- 原五分钟 `AnimeService-CodexDispatchGuard` 增加只读准备诊断，写 `data/diagnostics/subtitle-recovery/status.json`，不创建 Engine、下载器或正文 owner。台账损坏明确显示异常，不当作零次失败。
- 原十五分钟 automation-2 读取 `status.recovery`，每轮推进一个实际修复动作，制作槽有空且有 ready 时优先按原票据派发。准备工作不阻止其它集交付，不创建重复自动任务。

`scripts/subtitle_recovery.py status` 返回 `actionable / overdue / retry_wait / source_wait / repair_blocked / needs_user / waiting_media / protected / ready`。有媒体盘点时间才据此计算六小时处理提醒；refresh/revision 不重置等待。六小时要求实际修复或说明，不代表质量豁免、强制成品时限或无尝试就让用户代劳。未到媒体继续等待原后台，不把未发布算故障。

已有熟肉到料而洁净母片尚未完成时，规划只在本集原 `subtitle_samples.evidence.workflow_role=clean_base` 对应唯一合法、`acknowledged=1/status=downloading` 的原任务，且当前范围、来源、单集 manifest 与归属均相符、无 owner/冻结/已看/租约/未知回执或对话框 hold 时，显示 `waiting_media/clean_base_download`，等待原后台完成后核验文件与画面。此分支只读，不发下载、不绑定、不改 input，保留原 condition、六小时 deadline、失败次数和冷却；下载结束后恢复原门禁规划。intent、uncertain、error、缺失或冲突回执不当作正常下载等待。没有上述确证时，洁净路线提示“核验洁净母片来源，完成后经受控入口绑定”，不宣称已有本地洁净文件；绑定仍须原完成回执及全部保护核验。

同一 job、input、实际动作路线、门禁原因和 production blocker 为同一故障条件；同路线实际失败后至少冷却三十分钟、最多两次。已有正文归档 collect_chinese_source、已有 OCR 校对 review_chinese_ocr、跨版对轴 align_chinese_source 和缺件查找 find_chinese 分别处理，搜索失败不封死已有材料的校对路径。缺受控入口或同路线失败耗尽时记录具体自动处理阻塞，继续其它合法路线；当前字幕路线耗尽记 source_wait，工具等自动修复受阻记 repair_blocked；不因缺稿/待校对升级用户。当前仅 unknown_dispatch 缺原派发回执事实保留 needs_user，不为凑次数重复失败。新输入/真正门禁变化可形成新条件，单纯刷新不清零；每轮还须遵守原单 owner 每24小时最多两次恢复的上限。修复失败记证据，不能用 status、refresh 或重读旧报告冒充尝试。

```powershell
./.venv/Scripts/python.exe -X utf8 scripts/subtitle_recovery.py record JOB --action ACTION --expected-input INPUT --expected-condition CONDITION --outcome failed --evidence data/subtitle-studio/PRIVATE/result.json
./.venv/Scripts/python.exe scripts/subtitle_recovery.py notice-delivered NOTIFICATION_KEY
```

证据只接受私有字幕目录内的小文件，绑定 SHA，同一证据不重复计数；不得改历史证据伪造第二次尝试。成功登记改变门禁后以新状态为准，不能拿旧条件强写成功。仅 `unknown_dispatch` 的 `needs_user` 且 `notification_pending` 原派发对账事项合并通知，用户可见地说明作品/集数、实际尝试与需要的授权/访问/回执事实，再记 notice-delivered。字幕正文、翻译、听核或 OCR 校对均由 Codex 处理，不要求用户代劳。无变化安静，禁止先标已通知再输出。

## 实际修复入口

未冻结、未看、无 owner/dispatch/lease、范围有效且真实完成归属合规的同集候选，允许 `scripts/subtitle_delivery_queue.py bind-target` 先 dry-run 再执行。它是独立制作母片选择，不改原视频/default。洁净画面、字幕及输出证据须绑定新 fingerprint，不能挪用其它母片报告。已有冻结/owner 不适用。

本机音频故障只允许恢复已登记的 `127.0.0.1:11436`，沿 `scripts/repair_subtitle_audio.py --output data/subtitle-studio/recovery-repair-20261021/audio --apply`。它核 executable SHA、原模型 manifest、已安装 blob 大小；无安装、pull、付费、云端音频或代理变更。健康服务不重启，端口被占不杀进程，启动前记意图；启动结果未知且原 PID 尚在、或意图无回执，禁止重启。已证明原 PID 消失且端口空闲才可另记一次启动。服务恢复仅证明通道可用，原负责人须实际验证新审听/对齐证据后按原授权受控 resume；不自动唤醒用户明确停止或旧大图聊天。

`committing / unknown` 下载或派发必须经原后台/原宿主回执对账，不直接清队列、改文件归属、补造 ack 或重新提交。无安全对账入口应记录工程阻塞并通知，不能永久显示普通“准备中”。已看和授权外只保护，不借此扩张范围。

所有新 v0/v1 仍须逐条中文覆盖核验。魔王 E4 的25条未对应中文不能以服务健康或无可靠日文为由豁免。

本轮实际修复证据在 `data/subtitle-studio/recovery-repair-20261021/`：学生会 E3、外挂 E3 已受控绑定原已下载候选；学生会 E3 已登记实际抽帧洁净证据；音频11436已恢复并回读原模型音频能力。原调度已将魔女 E3 推进至制作，不能重复派发。登记和通道恢复均不等于新成品交付。

## 已确认下载回执被误判的修复

女仆 E7/E9/E10/E11/E12 在原迅雷只读状态中均核到唯一原 TaskId、acknowledged=1 和 verified_complete=true；`thunder_queue.state=done` 仍保留 committing=1 是原桥接器的点击历史，并非未知结果。旧字幕材料、候选绑定、预览及自动盘点四处一律拒绝 committing=1，误将已完成原回执永久阻塞。

统一 `anime/download_receipts.py::pending_receipt`：仅 provider=thunder、task.status=complete、acknowledged=1、非空原 external_id、queue.state=done、无 lease 的组合允许历史 committing 标记；任何缺失、pending/claimed/review、活跃租约或未确认对话框仍阻塞。保留历史字段，不清任何未知回执，不修改桥接器或下载器。实际原回执归档 `maid-original-thunder-receipts.json`，E8 已从失效旧路径经 bind-target 切换至现有完成候选，回执 `maid-e8-bind-False.json`。
