# 普通媒体版本证据：发行与迁移前置契约

状态：owner审查设计，**未实现生产发行、bootstrap、迁移或启用**。隔离实现见playback_versions.py及PLAYBACK-SESSION-CONTRACT.md。没有新增入口、生产表或后台扫描；本文件不授权手工灌入进度/版本表。

## 已核实的输入边界

WebPlayer普通媒体key是现有路径的小写SHA-256前32位；episodes/inventory提供路径、大小与作品集号，item校验存在与大小。Engine.reconcile对qB的既有拥有者、save_path、选集manifest、文件名/大小/progress/state做校验；verify_qbit_files校验torrent manifest与选择。它们没有提供媒体文件完整SHA-256或不可变发布承诺。torrent infohash、任务状态complete、qB progress=1、文件mtime均不能直接充当版本SHA-256。

因此不能把这些现有字段填入OrdinaryMediaVersions.register(expected_sha256)，也不能由客户端提供hash，或在首次播放时对“此刻恰好在路径上的文件”自动建立可信基线。

## 下一步发行器应满足的边界

1. 输入是受信发布过程产生的不可变内容清单，而非当前路径自证：完整媒体SHA-256、大小、授权作品/本地集/季映射、原任务/选择清单签名、发布者/版本、发行时间与唯一发行ID。绑定server实际inventory/canonical key与映射。若人工导入旧媒体，必须是单独审查的owner发行流程，不能伪造为原下载完成时的证据。
2. 在后台、受限并发/IO预算内读取实际完整字节。无法证明稳定输入、超预算、映射或文件变化、过期签名都拒绝；不由HTTP请求触发大文件补证，不改下载器/作品范围，不把快速stat缓存当内容证明。
3. 原片保留。需明确选定不可变发布存储/写入者隔离，或受控句柄在验收窗口阻止更换/写入的Windows实现；普通Python stat/hash前后相等只描述读取窗口，不保证后续播放/DB提交期间不被换。采用复制时先审查空间/并发/原始文件边界，不自动搬移或删除旧媒体。
4. 注册提交以不可变清单摘要为幂等身份，同key重试必须同path/size/hash/association；冲突保留原证据并拒绝重绑。不将磁盘访问与SQLite事务称作跨资源原子；需定义中途崩溃时的只读计划/可重试步骤及核验快照，禁止持有DB写锁等待大文件hash或网络。
5. 首次登记不追认既有legacy web_progress的历史版本：旧位置的版本来源未知时，保留原行但不能自动宣传为已验证的同版续播。必须单独定义首次v2迁移时的读取/恢复政策与用户可见行为，不能猜测回填或批量重置。未解决前普通媒体生产v2继续拒绝。

## 版本与别名

- 同一已证版本的各种请求别名必须解析到同一canonical progress row；原始位置/观看记录保留，不复制成别名行。
- 同路径换成不同内容时旧key永不重绑。新版本必须由可信发布过程获得独立不可变身份/受控新路径，并经过实际解析/来源选择契约；旧alias继续拒绝或仅指向保留的旧版本，绝不静默指向新内容。
- 目前隔离实现仍使用路径key、没有完整的新版本发行/旧alias恢复流程；这是一项阻塞，不能只补一张hash表就开启。

## bootstrap与验收门禁

将来由owner显式构造同一Store的真实WebPlayer、版本库、resolver、PlaybackWriters和HTTP adapter，默认off；schema迁移、回滚保留generation/fence、错误可见及可信callback需一起审查。研究侧客户端仅消费可选bootstrap.capabilities.playback_writer，字段未实现/未宣传时不发v2请求；client不创建服务端表、不负责发行证据。

当前callback是Store级全局开关，开启会拒绝所有legacy进度写。因此不能在只有部分普通媒体证据可用时全局开关、再让不支持资源退回legacy；需先完成范围内覆盖与客户端就绪，或另行设计经过审查的按资源门禁（本轮未做）。

最少验收：真实大文件耗时与预算可见失败；发行期间原地写/rename/删除/映射变化；完整字节一致但file-id变化；清单伪造/重放/错映射；崩溃在hash后/证据提交前后的重试；首次legacy位置来源未知；旧alias与新版本独立、同版别名同一行；混合客户端/feature-off与回滚仍受持久fence保护。全部在隔离库/合成或专用副本中做，未经后续明确部署步骤不创建生产表或回填进度。
