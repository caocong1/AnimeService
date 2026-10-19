# 字幕制作停滞恢复

2026-10-18：用户要求修复制作停住。第8集已有v0，但原负责人轮次实际中断，宿主仍保留production/acknowledged；调度误把“保护原owner”执行成长期只读检查。原轮最后的author/preview还报缺PIL。不能把成品存在、调度正常运行或owner已确认当作制作仍在推进。

## 分开判断

- 应用active且有新工具/输出：跟踪同owner，不重复发继续。
- 应用notLoaded/idle本身不证明停止；核精确thread、latestTurn及最后工具状态。interrupted/failed表示该轮未继续，不说明用户有意停止，也不证明外部工具没有遗留进程。
- 明确用户停止、未知派发/回执、committing、有效租约和其它writer仍保留。中断原因或停止意图不明时，保存具体阻塞并明确通知需要何种恢复授权，不能在后续轮次一直把它称为正常活动生产。没有新事实不刷通知。
- 用户明确要求恢复后，核原job/input/thread、逐集未看、范围、当前工具/编码进程与应用不再运行；保存真实应用回执。原owner确已停止写入时可记录blocked --stopped（仅表示实际停止写入，不伪造用户停止意图），再begin，只按返回resume恢复同聊天并confirm。不要创建第二writer、直接改SQL或伪造v1票据。原用户停止的魔王大图owner不能沿这条一般修复自动唤醒。

本次用户新修复要求已明确授权恢复529723/E8，已完成同owner的begin/resume/legacy confirm，回执在`data/diagnostics/subtitle-stall-repair-20261018/`。原input、v0与观看保持，恢复不能当作v1交付；后续调度回读实际新turn和finished，禁止重放本次恢复步骤。

## 依赖与派发

字幕制作使用项目.venv。Pillow现已加入requirements和lock；恢复前实际加载现用CJK字体并测宽，不仅核import。环境重建后在制作前检查真实字体/ffmpeg/浏览器输出能力；工具失败保存stderr并报告具体阻塞。

Windows PowerShell 5会继承宿主PowerShell 7的PSModulePath，导致Get-Acl的Security模块自动加载失败。票据目录现在用同一.NET Directory的SetAccessControl/GetAccessControl设置并回读权限，不依赖Security/Utility模块；仍只允许当前用户和SYSTEM，不降低ACL标准。begin先验证票据目录，再申请租约/写意图，避免已知ACL错误先占槽。真正写票据时仍再次校验，发生后续失败时保留committing，不重复begin或猜造票据。

本次首次恢复begin在意图已写入后遇到旧ACL代码错误，尚未发外部消息。修复后保留该意图，只发送一次原聊天恢复并根据真实成功工具回执走现存legacy confirm；没有补造v1 issued_at/ticket，也没有开启新回执强制模式。此处是本次已知失败的恢复记录，不授权未来借当前token套用旧callback。

## 后续准备

唯一制作槽有人工作时，调度仍可完成下一未看集的只读材料核实、真实洁净检查和工具能力准备，沿既有readiness路径登记；不得因此抢占writer、反复下载或把准备写成完成。v0发布后须按实际player报告调用finished，回执continue_refinement=true就由原writer继续v1；v1真正完成后才释放槽并按原优先序派发后集。

## 显式 created 事实的人工对账恢复

原始 created 回执因 revision 变化被拒绝时，先用只读 reconcile-plan 核原 ticket、拒绝回执与当前事实。只有用户明确授权、原负责线程已停止写入、范围/未看/固定输入仍满足，才可由 owner 使用 confirm 的显式对账参数；协调轮不得因看到旧拒绝回执而自动恢复。

恢复材料须保留原 ticket 和原拒绝回执，另归档新鲜的原生宿主读回结果，绑定同一 job、input、负责线程和明确的当前 revision。宿主 cwd 必须显式为绝对路径并属于项目；缺失或相对值不得从调用进程补齐。原始响应、真实线程/turn 关联和恢复目录只保存在私有审计区，不写入公开文档或提交信息。

对账只确认真实聊天归属，不创建聊天、不替旧回调补当前凭证、不证明 finished/accepted。成功时 job、派生事件和回执在同一事务应用，原拒绝回执不变；重放只返回首次结果。范围、已看、停止写入、后续 begin、线程冲突和其它 owner 的保护不得绕过。

跟踪原 owner 的后续进展只读真实新 turn/tool 与 finished 事实，禁止重放同一恢复流程或循环执行 blocked/begin/resume/confirm。edition、finished-observed、正式审阅和 accepted 分开记录。宿主材料缺失、不新鲜或身份不一致时停止，交 owner 重新核验；不得清回执、改 owner/lease 或伪造缺席证据。
