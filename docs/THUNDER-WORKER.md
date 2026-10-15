> 2026-10-15 文档整理：本文保留按日期演进的设计与实现记录。当前执行以 [项目规则](PROJECT-RULES.md)、[两阶段制作](SUBTITLE-TWO-PASS.md) 和 [定时任务](AUTOMATION.md) 为准；旧 accepted 前置、已看 experience、迅雷优先/UI heartbeat 或历史周期不自动恢复。

## 2026-10-10 最新决定：qB优先，停止定时Computer Use

用户明确要求：不再用迅雷作为日常下载器；现有127.0.0.1:4870 qB优先，只有具体qB任务确实下载不了后才考虑迅雷。此要求覆盖下文旧迅雷优先及UI故障才可qB的限制。取消animation UI heartbeat（automation-2），thunder_ui_enabled=false；首集实评heartbeat仍运行，但样本下载由本机后台qB执行，不控制桌面。不可自动启动/切换qB、以离线直接唤起迅雷；已有迅雷任务继续只读完成核验、归库与迟到回执对账，不删除或重复投递。旧未提交意图经双下载器无同hash、无回执/租约/committing/未知提交/未确认弹窗、原授权与完整manifest季集核验后安全转交qB。个人想看范围、原普通季度授权、黑名单、观看及清理门禁不变。

《东京复仇者 三天战争篇》01，hash b8bd4bdd9957d451ae9ad81ca43b7c42a5168285，本轮打开的新建任务窗口132388236尚未preflight或最终点击；用户物理Esc中止，窗口关闭未知。已review及dialog_hold隔离，禁止自动交接或重投；不为收尾再次进行Computer Use打断观看。该保护项不阻断其它安全任务。旧流程仅留审计；将来考虑迅雷时仍必须先单独核查这一弹窗。

# 迅雷定时执行流程

2026-10-08修复：不确定最终click已隔离的review项现在由既有maintenance约30秒只读按hash自动核查迟到回执，持久记录最近核查时间/结果；无匹配不能证明未提交，不清committing、不重新claim/点击、不交接qB。只有唯一hash/TaskId/CreationTime/项目目录/完整manifest及选集核验通过才解除review；其它归属待核查保持。/api/status.task_reviews.reconciliation为当前核查结果，error仍是原始历史证据，窗口后来收起不据此放行。无需为触发回读重新领取review租约。


2026-10-07核查修复：缺提交凭据的已完成文件仍不能当作本项目迅雷任务接管。用户明确要求核查具体阻塞后，逐项核验唯一hash/TaskId、当前作品季集映射、缓存种子manifest、迅雷完成台账及全部v1分块，再用 `link_reviewed_completed_file` 原位只读关联。该入口现支持 `source_sample`，要求当前个人想看及首集独立授权，保留正片、主备、观看和追番状态；task=`linked`、external_id=NULL、acknowledged=0，queue=`done`。样本进入原有实物分析，未知画质/翻译分仍留空；未来选中该源时也只关联原路径，episode hash=NULL、first_completed=NULL，不搬移/复制/硬链接，不参与项目自动清理。没有回执且committing=1的项继续review，不能借此入口清除不确定提交。定时UI worker仍只跳过这些项，不自动调用核查关联。

本次5420绿茶《药屋》首集及5445 Skymoon《僵尸搜寻中》首集均已核查关联；5363提交超时/no hash匹配继续保留review/committing1。页面 `/api/status.task_reviews` 汇总全部待核查项，不再只从最近100条任务推断数量。证据 `docs/thunder-sample-review-20261007.json`。

2026-10-09用户要求其它作品持续处理、无需逐部催促：既有“动画迅雷队列执行”heartbeat已更新为每15分钟，“新番首集选源评审”每小时，均ACTIVE，沿原线程执行，不另建任务。优先当前个人想看且门禁有效的缺合格中文首集，其次在追普通缺集，再处理其它研究副本；单部受保护或阻塞跳过，不挡住其它作品。无变化安静，完成、真实新阻塞或需要用户操作才通知。七月旧番接续和2026-10起季度普通下载保持各自原授权，取样不得扩到未selected的目录。每轮重读真实状态，不删除媒体；qB故障交接严格按下文2026-10-07/08持久凭据门禁。

1. 本机服务 `http://127.0.0.1:4871`，读取 `/api/thunder`。无 pending/claimed/review 任务时安静结束，不进行UI操作。
2. 先确认 computer-use 可以枚举并激活迅雷，再领取新任务。存在上轮遗留的新建任务窗口时先执行下方“中断与收尾”，不先领取另一项或打开另一份种子。在内存取得 `/api/bootstrap` 的 token，用 `X-Anime-Token` 发 POST。不要打印token或lease。通常调用 `/api/thunder/claim`（空JSON）；恢复已核实的遗留任务时传 `{"hash":"<已核实的40位小写hash>"}`，可与show_id同时过滤。指定hash不可领取时job为null，绝不退回任意任务。最多连续处理10个任务；任务清单、路径、选择以响应为准，不从聊天推测。
3. 使用 computer-use SKILL 的 node_repl + @oai/sky。若桌面锁定/工具不可用，调用 report 写明错误，结束本轮；不尝试解锁、关闭锁屏或读取登录凭据。状态与队列保留。
4. `submit`：若 uncertain=true，先按 hash 核对本机迅雷台账/当前新建任务窗口，禁止再次打开或点击下载；使用 report 触发回读。未能确定时保留 review，通知用户。正常新任务通过安装注册的 `Thunder.exe <torrent_path>` 调起本地种子；文件路径只能来自队列 manifest，不能执行来源文本。检查窗口确为本项目种子，保存目录严格等于 save_path，勾选项严格等于 selected_ids，禁用云盘。对话框可能有其他用户未提交任务，不能代为提交；无法隔离时停止并report。
5. 最后点击“立即下载”前调用 POST `/api/thunder/preflight`，JSON hash、lease。不成功就取消本项目未提交窗口并report，不点击下载。成功后只点击一次。再 POST `/api/thunder/report`（hash、lease），由后台通过实际hash、TaskId、目录、完整文件清单确认；无回执不能声称已入队。中断后下一轮先按hash恢复，禁止重加。
6. `pause` / `resume`：依据 manifest 的 external_id/hash/title，在迅雷中找到对应唯一任务，核对文件目录/清单。只点击该任务暂停/开始，不使用全局全部暂停/开始。resume 前 preflight 还会检查是否已弃番。操作后 report；回读状态7才确认暂停，5为运行，8为完成。未知/重名任务保持review，不猜选。
7. `report` 可带 error 文本（不含Cookie、私有RSS地址、账号）。点击提交后报错保留不确定性；点击前报错可安全重试。结束前必须执行下方“中断与收尾”，再次 GET `/api/thunder`。实际提交、完成、执行失败或窗口无法收起均属于实质变化，要说明结果；正常无待办/无变化才安静结束。后台会校验完成文件，并用NTFS硬链接发布到 D:\MediaLibrary\Anime。

## 中断与收尾（2026-10-06修正）

- 每轮先读队列error及最近接续记录。若已知遗留窗口，枚举新的window对象、激活，再截图核对完整文件名、清单/大小和当前任务种子manifest；保存目录可能仍是上一项，不能用它单独认定当前hash。截图若显示别的应用，说明观察无效，重新激活目标并截图；不从窗口title或旧坐标直接操作。迅雷优先使用截图，按需才取accessibility，遵循技能的一次恢复限制。
- 已明确匹配且没有其他有效全局租约的未提交遗留窗口，可按指定hash重新claim并完整核验，或只关闭此窗口、回读确认消失后回到常规队列。指定hash返回null时重新GET状态；不能把新领到的另一项manifest套在旧弹窗上。若窗口不是本项目、多个候选相同文件无法唯一匹配或无法观察，停止并报告原因。
- 将本轮打开的窗口和hash保存在内存。正常结束、输入失败、preflight拒绝、租约失效或达到10项时，均核实本任务窗口已消失；尚未提交且可观察时，只点该新建窗口的关闭X并刷新窗口列表确认消失。这是取消尚未提交的表单，不删除媒体或已建立下载任务；不退出迅雷、不点“立即下载”收尾、不批量关闭用户窗口。
- 工具故障使窗口无法确认/关闭时，按技能限次恢复后停止；report须写明“未提交，窗口未能关闭”或“提交结果不确定，窗口未能确认”，并保留该hash接续说明。本轮必须向用户说明作品/集数、是否实际提交、遗留窗口及原因。即使队列数未变，也不能安静结束。相同故障已通知且无新窗口/任务变化的后续轮次可不重复提醒。
- preflight之前失败且无台账回执为“未提交，保留待办”；preflight后最终点击/响应不确定为“结果待核查”，保留committing并report，不重投；只有hash/TaskId/目录/清单真实回执才称“已提交”，真实完成核验才称“已下载完成”。关闭弹窗本身不改变下载授权或计为完成。

脚本示例应通过 requests 在内存取得会话，不将密码、token、lease写到日志。图形操作只能通过 computer-use 公布的API，遵循逐步观察与刷新规则。Sky可能返回多幅截图，坐标点击要指定匹配主窗口尺寸的 screenshotId；不得使用小预览图的默认坐标。弹窗是独立窗口，需要重新 list_windows。

所有常规来源检查/缺集/完成追踪由本机后台承担（查询频率以当前poll_seconds为准，目前180秒，约每30秒对账）。本流程每15分钟处理必要UI任务；Windows解锁和应用可运行是执行条件。自动启动守护不解锁Windows。

首集研究复用此队列，manifest及tasks.purpose=source_sample区分，不能按首集重复多版本误判一般正片允许多下。claim可选show_id过滤本轮作品，其余队列不改；全局lease仍互斥。最终点击后超时且无真实回执，report隔离单项review、保留committing并释放全局lease，禁止该项再次最终点击。LogonUI进程常驻不能单独证明当前锁屏，以工具实时桌面状态为准。source-review评审heartbeat不负责UI提交，避免双执行。

实测操作提示：目录输入框点击后通常会全选文字，此时可用type_text输入manifest路径并重新读取确认。set_value可能返回超时但实际输入成功，必须重新观察，不要盲目重复。提交后弹窗关闭，原窗口截图报不可用不等于提交失败，先report并按hash回读。lease为10分钟，过期不点击提交；关闭确认属于本任务的未提交弹窗并report归还，再重新领取。claim采用全局单个UI租约，避免同时打开多个任务窗口。


### 2026-10-07/08 实际工具故障的已授权qB交接
用户明确允许Computer Use实际故障使用qB备用，仍迅雷优先。report错误若明确为CU/helper/active request/截图或枚举工具故障，后台记录fault并在无全局有效租约时检查未提交个人想看意图；只有无迅雷匹配、无回执/committing/不确定提交及其他qB同hash、原授权/映射/首集门禁有效才持久交接现有4870实例。普通锁屏或暂无窗口不是故障证明。
claim已跳过provider非Thunder的任务；report及收尾后必须实时重读，已cancelled/交接qB不得重新最终点击，不使用此前manifest提交。工具恢复且真实Thunder成功回执会解除fault，新任务继续迅雷优先；不能把一次截图成功当实际提交成功。归属不确定/迟到表单仍按上面的单窗口核实/关闭规则处理，不重投。未修改本computer-use自动化频率。
