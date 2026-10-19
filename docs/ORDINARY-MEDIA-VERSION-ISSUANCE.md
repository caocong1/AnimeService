# 普通媒体版本证据：发行与迁移前置契约

状态：owner审查设计，**未实现生产发行、bootstrap、迁移或启用**。隔离发布接口已在ordinary_issuance.py实现，版本库见playback_versions.py及PLAYBACK-SESSION-CONTRACT.md。没有生产构造点、新增入口、生产表或后台扫描；本文件不授权手工灌入进度/版本表。

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
- 新隔离发行器以完整内容SHA、大小、association_digest生成版本身份，发布到专用objects/<version>/video.bin；其路径key是未来canonical身份。相同内容和映射的多个原始alias只能得到同一发布key；原始alias的签名清单只允许首次创建、之后同值重试，不提供更新/删除接口。不同内容或映射必须另有受信输入身份，不能复用原始alias。
- 这还不是WebPlayer/resolver/版本库的生产接线：现有inventory不会自动识别发布路径，不能把发布key当作已经可播放或已有progress row。接线前须在同一Store隔离验证“多alias→同一canonical row”；不同版本独立row，不迁移未知legacy位置。

## 隔离发行接口与证据

OrdinaryIssuer只允许显式注入受信source验证密钥、独立publication密钥、已存在的受限目录与server resolver。它不签发source证据，不从当前路径自动学习可信SHA，不读取HTTP参数、生产配置或qB状态，不导入Engine/创建DB。尚未实现独立证据生产者、密钥部署/轮换或人工导入命令。

源证据ordinary-source/v1使用HMAC-SHA256认证（密钥至少32字节；这是受限对称验证边界，不是公开签名）。规范化UTF-8 JSON，排序键、紧凑分隔、拒绝非有限数；MAC绑定schema、key_id和payload，域分隔ordinary-source/v1+NUL。payload固定为issuance_id（32hex）、issued_at/expires_at（Unix秒整数，最长24小时）、source_key、完整sha256、size、association_digest、input_signature、publisher。源SHA必须来自独立受信发行过程，不能由发行器对任意当前路径自签。resolver必须重新执行真实授权/映射/原任务选择清单门禁，input_signature是这些输入的权威签名；合成fixture不等于生产实现。

返回PublicationLease仅在with窗口有效；退出/超时拒绝读取或check_binding。Windows本地固定盘使用CreateFileW只分享FILE_SHARE_READ，打开源、副本、清单和alias，拒绝已有/后续写或删除冲突；全局发布锁share=0、冲突立即失败，不抢占现有写入者。源与副本都完整验SHA，重试也不跳过原片；拒绝reparse/ADS/非普通文件。官方共享契约见[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)。

这保证的是持有句柄的验收窗口，**不是退出窗口后的永久不可变承诺**。专用目录及祖先必须由owner独占写入、禁止其它发布者和回滚/删除alias；本轮不设置生产ACL，也没有抗恶意存储回滚锚点。签名能发现修改，不能发现整个alias历史被删除；不可信或可回滚目录不能用于生产。后续播放/版本注册必须继续持有受控句柄或重新完整验证，不能依赖一次hash缓存。Key轮换须保留历史验证能力，缺旧key即拒绝。

默认单发布者、8GiB字节上限、120秒合作式时限、64MiB空间预留；构造参数最多64GiB/600秒。源复制与副本复核共用预算，1MiB分块检查；超预算/磁盘不足可见失败。时限不承诺能中断内核单次阻塞IO，也不在HTTP热路径运行。生产前还需专用大文件性能/磁盘预算验收，不能用合成小文件冒充。

## 发布、崩溃与数据库边界

先在专用根的随机独占pending目录写副本/完整清单并fsync，再rename到不可覆盖的版本目录；最后以临时文件+独占hard-link发布alias，不存在部分JSON。重试验证现有对象与清单，不覆盖损坏/冲突对象。失败只清理本次创建的pending固定文件；不删除原片、已发布版本或别人的pending。崩溃可能留下孤儿对象/pending，后续只读列计划，不能自动清旧媒体。rename/fsync不被描述为断电后的完整事务保证。

大文件工作全在DB事务外。组合顺序：取得live PublicationLease → 短Store事务读当前授权/映射/输入 → 幂等登记版本 → 提交前再次assert_live → 提交后释放句柄。check_binding不查文件/活库/IPC，不在DB写锁内等待hash。此流程仍非文件系统与SQLite原子事务；崩溃后对象已发布但DB未登记应可验证重试，不能凭磁盘孤儿推导进度或观看事实。

隔离登记接口现为`OrdinaryMediaVersions.register_publication(c, lease, current_source)`：只接受该Store的现有事务与真实live PublicationLease，消费lease.proof()的不可变已核验快照，核current_source后同key同值幂等插入/冲突拒绝。不打开新事务、不commit、不查文件/hash、不调用外部resolver；current_source必须由受信调用方使用同一c读取真实授权、映射与输入事实，绝不能来自HTTP或事务外旧快照。这个权威facts builder尚未实现生产版本；接口无法替调用方证明传入dict来自何处。现有无lease的register仍仅供原隔离用法，不宣称与新接口具有相同窗口保证。

调用方须在lease上下文内完成事务提交，提交前assert_live，任何异常回滚；不能拿返回key当作提交完成/已登记inventory/已保存进度。proof使用的canonical路径、SHA、大小、association为内部不可变快照，不采用可变展示属性。隔离测试已经用真实临时Store、WebPlayer、PlaybackMediaResolver与PlaybackWriters证明两个已证source alias登记一个版本/一个canonical进度行；登记本身不新增进度、不把未知legacy位置迁移过去。该测试的合成episodes登记仅存在临时库，不是生产导入流程。

## 默认off组合与迁移验收矩阵

生产组合尚未实现。建议按以下门禁逐项提交审查，默认关闭到全部通过：

| 阶段 | 前置/操作 | 必须验证 | 失败/回滚 |
| --- | --- | --- | --- |
| 存储与证据 | 独立SHA签发源、密钥生命周期、受限目录/防回滚边界 | 真实授权、选集签名、完整内容绑定；大文件预算 | 拒绝发行，不由播放请求补证 |
| 身份接线 | lease、真实WebPlayer、OrdinaryMediaVersions、resolver共享Store | 同版alias同row；替换/删除/映射变化拒绝；新版本独立row | 保留旧证据/原进度，不重绑 |
| schema迁移 | owner显式迁移三个表，记录schema/备份/计划 | 空库/旧库/重试/崩溃恢复；不回填未知legacy版本 | 仅停止宣传；不删除generation/claim幂等记录 |
| 组合默认off | 真实可信playback_writer_enabled回调、HTTP adapter、既有auth/origin | 关闭不构造三组件/表/路由，不接受客户端开关 | 保持原路径；已有持久fence不可绕过 |
| 首次进度 | 定义未知legacy位置的可见策略 | 不自动当作已证同版续播、不批量清零/回填 | 用户显式恢复/选择，原行保留 |
| 客户端就绪 | Web/TV显式claim、结构化冲突/错误、原观看租约 | 跨端/混合版本/迟到响应/并发序号/切源/pagehide | 已协商v2后故障停写，不能回落legacy |
| 能力宣传 | 全范围证据覆盖与客户端验收后owner显式开启 | bootstrap唯一正协商、共享Store回调与legacy事务fence一致 | 停宣传不清持久fence；旧版本回滚也拒绝绕过 |

当前全局Store回调会拒绝所有legacy写，因此部分媒体支持时不得全局启用；按资源能力需另案审查。默认off组合代码也必须证明关闭时不创建三表，不能仅隐藏UI。TV真机/签名发布另列前置，不用debug CI替代。

## bootstrap与验收门禁

将来由owner显式构造同一Store的真实WebPlayer、版本库、resolver、PlaybackWriters和HTTP adapter，默认off；schema迁移、回滚保留generation/fence、错误可见及可信callback需一起审查。研究侧客户端仅消费可选bootstrap.capabilities.playback_writer，字段未实现/未宣传时不发v2请求；client不创建服务端表、不负责发行证据。

当前callback是Store级全局开关，开启会拒绝所有legacy进度写。因此不能在只有部分普通媒体证据可用时全局开关、再让不支持资源退回legacy；需先完成范围内覆盖与客户端就绪，或另行设计经过审查的按资源门禁（本轮未做）。

最少验收：真实大文件耗时与预算可见失败；发行期间原地写/rename/删除/映射变化；完整字节一致但file-id变化；清单伪造/重放/错映射；崩溃在hash后/证据提交前后的重试；首次legacy位置来源未知；旧alias与新版本独立、同版别名同一行；混合客户端/feature-off与回滚仍受持久fence保护。全部在隔离库/合成或专用副本中做，未经后续明确部署步骤不创建生产表或回填进度。

## 第34轮：可信输入、密钥与锚点的隔离第一增量

新增 `ordinary_attestation.py` 与 `ordinary_anchor.py`，均无生产构造点、HTTP/CLI、Engine、数据库或网络。上述“尚未实现独立证据生产者/轮换/锚点”的历史描述，现细化为：已有隔离组件和合成测试，生产许可/facts builder、实际密钥部署、外部锚点与发行器组合仍未接线，不能据此启用。

### 谁在何时签发什么

只有受信控制面的 intake 协调者可准入两种许可：`custody-handoff/v1`（完成选定输入的独立字节交接审查后），或 `owner-reviewed-import/v1`（owner明确审查并接纳旧媒体的新观察，不冒充历史下载完成证据）。许可有独立intake_id与明确approved；不是媒体路径/key、qB完成状态或HTTP请求。`SourceAttestor.attest(intake_id)`仅向受信approve_intake回调取当前许可、授权范围、服务端规范路径/大小、association_digest和input_signature，再持有Windows拒绝写/删共享的稳定只读句柄读完全部字节；签发前重新核许可与输入、句柄/路径身份和预算。失败没有envelope，原片保留。

可信SHA来源是这个有明确准入、时点、范围和持有句柄的独立交接观察。不能把任意当前路径hash、mtime、infohash或下载完成状态替代许可。签名只能证明受信生产者在该准入窗口观察到的完整字节，不证明交接之前的历史来源，也不保证释放句柄后的永久不可变。新证据仍由OrdinaryIssuer二次核当前输入并复制/复核，不能跳过原发行器。

生产approve_intake尚未实现：必须由同Store权威事实产生，核个人选择/独立授权/cleanup与已看边界、真实任务及所选文件manifest、季/本地集映射、输入版本和交接决定。input_signature必须规范化绑定这些事实以及intake_id、mode、许可修订，不能信任调用者给的签名或复用过期dict。SourceAttestor内不读qB/HTTP，也不替callback证明事实来源。当前只有合成许可测试；不得拿本轮对真实文件的性能测量当作该许可或签名证据。

envelope保持ordinary-source/v1和原固定字段/MAC域，现有OrdinaryIssuer.source可验证其密码学及结构；使用TimedOrdinaryIssuer时另加密钥当前有效区间检查。首次HTTP播放不能生成许可/补证，旧key不重绑，未知legacy进度不追认、不搬迁、不清零。生产调用方必须把准入证据与签发envelope关联保存，后续源证据格式如需显式携带许可详情需另审，不能静默扩展现固定字段。

### 密钥生命周期与专用目录

TimedKey用系统随机32字节密钥、随机key_id，source/publication角色隔离，Keyring拒绝重复id和跨角色复用密钥材料。签发key_id显式选择；生效区间为[not_before,not_after)。源签发时issued/expires必须落在密钥期限内；验证时当前时间也必须仍在期限内，包括历史manifest中的源证据，不能靠历史at参数复活过期密钥。轮换保留旧验证key至约定重叠窗口，窗口内旧source/publication可验，过期立即拒绝；缺key或未知key拒绝。

这意味着不能靠删旧key完成无限期历史保留，也不能无限延长已过期key。生产需要确定足够验证窗口与经过审查的历史checkpoint/重签迁移；过期历史链目前fail closed。这是上线前阻塞项，不宣传为自动轮换服务。

密钥生成/保存仅有显式模块API，**本轮未生成或部署生产密钥**。新目录独占创建后才设受保护DACL，仅当前用户和SYSTEM；不收紧既有父目录。密钥写前校验继承ACL，仅这两个主体FullControl，目录/文件所有者也必须属于这两个主体，防止其它所有者通过隐含WRITE_DAC改权限；临时独占文件写/flush/fsync后独占hard-link暴露完整目标，不覆盖旧文件，读回再验ACL、规范JSON与结构。密钥repr不含secret，不打印密钥或异常子进程输出。HMAC验证者也能签名，故该边界是受信本机控制面，不是公钥认证或同账户恶意进程隔离。

实际部署须由owner单独准备专用路径/祖先写权限、签发者与验证者职责、密钥清单及轮换/到期/撤销计划，在受限目录生成并离线核角色/key_id/期限，再显式装载；不从环境/HTTP隐式学习密钥，不改运行配置或启动后台扫描。

### 外部单调锚点与崩溃边界

AnchoredLedger是独立的签名append-only alias→manifest摘要账本；每条绑定schema、外部anchor identity、单调sequence、前一条完整摘要及不可重绑alias。读取必须验证整链与外部head；旧清单替换、缺行、重放另一序号、降级schema、签名伪造或同alias新摘要均拒绝。并发根锁不等待、不抢占。新记录独占临时文件flush/fsync后hard-link发布，随后外部CAS；CAS前异常留下完整pending事实，同值重试可完成；CAS成功但响应丢失时读回已提交事实幂等返回。不同pending事实拒绝，不能自动删除重写。

外部anchor必须提供不可回退的(identity, sequence, digest)持久根及原子compare_exchange；缺失/不确定/CAS未确认均失败。**没有生产anchor provider**，测试使用受控合成服务。旁边的head.json、同一份目录/磁盘快照、由账本自己签名的旧head都不能充当防回滚根；ACL也不能防当前用户将整个历史回拷。不得把本组件说成已解决整机/同账户恶意回滚。fsync/rename/hard-link不被描述为跨资源或断电原子事务。

当前账本尚未接入OrdinaryIssuer.publish、PublicationLease或inventory；下一增量须定义锁顺序，先验证已锚定alias不重绑，再在完整发布验证后提交外部CAS，且只有anchor已确认才向调用方交出lease。对象/alias与anchor不原子，崩溃后必须可读计划并以原受信输入重试，绝不让未锚定文件通过生产resolver。没有该组合时，单独使用旧发行器仍没有防回滚能力。

### 原机真实预算与第3项前置

数字证据留owner回执review34-budget.json，不入库真实文件标识、路径或SHA。测量只对三个现有1GiB级完整文件用现有Windows稳定句柄及1MiB块完整读取；不签发真实envelope，不复制媒体、不联网。记录1/2/3并发、合作式短时限、RSS、进程逻辑读取计数及全系统磁盘计数。缓存未驱逐，系统磁盘计数含其它进程，read_time不是可归因的占用率；不能当冷盘SLA、独占磁盘负载或完整发行复制性能。真实发行还需另验复制+副本复核、空间预留/磁盘不足、同卷并发、公平准入及实际设备预算。单次内核阻塞IO仍不能保证硬中断。

第3项开始前：落实受审生产intake/facts builder和input_signature定义、真正独立的anchor provider及密钥期限/历史策略、账本与发行lease组合及崩溃恢复计划、专用存储ACL/容量、真实复制预算。随后才能将同Store事实短事务、live PublicationLease及可信enablement默认off组合接线；事务内不等待hash/复制/网络/anchor IPC，必须先取得不可变已锚定快照和live lease，提交前assert_live。生产schema另有独立迁移步骤；本轮不建表、不回填、不宣传能力、不替换worker。

### 连续字节托管的生产阻塞条件

`custody-handoff/v1`不能只有“曾批准过此路径”的记录。受信交接者必须在独立准入决定之前已持有拒绝写/删共享的句柄（或有同等已审的不可变输入承诺），并一直保留到SourceAttestor完成签发；SourceAttestor可以在此期间打开同一文件的第二个只读句柄。签名完成后，后续publisher仍须用该完整SHA重新验证原片及副本，不将释放句柄后的路径状态当证据。`owner-reviewed-import/v1`则明确是owner接纳本次新观察，不证明旧下载历史。

本轮合成补验已证明外层Windows稳定句柄跨越准入与签发时，重命名被拒绝、源签名可验证；**生产连续托管协调者仍未实现**，approve_intake这个回调本身不能证明上述时序。因此本轮c是可审的隔离签发基础，不代表任意传入approved字典就有可信原始来源。真实回调、托管许可归档和协调者必须另审，不能接HTTP补证或跳过独立许可。
