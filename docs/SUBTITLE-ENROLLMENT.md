# 在追作品字幕流程漏接修复（2026-10-16）

用户随后明确更正：更新/重启无需等待播放空闲。此要求覆盖本文下方旧的空闲部署安排；本轮按明确授权直接重启既有supervisor管理的媒体后端，并核服务健康、watching_auto_enrollment及实际队列。保留观看租约/记录、原媒体/default/accepted和制作owner，不重启下载器、不重复未知获取请求。实际重启回执data/diagnostics/watching-subtitle-enrollment-20261016/authorized-restart.json。

用户本轮明确要求重骑士511177第15、16集及其它符合独立授权的在追漏接作品一起修好，先v0后同一负责人连续v1，并修复自动漏接根因。下一可用制作槽优先重骑士两集，顺序15→16；不抢占当前v1负责人、未知派发或有效租约。就绪后的单集创建、确认与跟踪继续执行SUBTITLE-DELIVERY.md。

## 登记和入队

原自动登记函数只遍历季度首集评审的review_cohort，并受source_review_new_shows/auto_quarter_enabled控制；continuing、较早季度和未再次进入cohort的状态转移均可能漏登记。生产调度只检查政策存在与否，却没有修复缺记录的路径，漏项因此长期保留。

现在`anime/subtitle_enrollment.py`在启用分流政策时，按selected/watching、独立授权、映射盘点及逐集边界登记缺失的`source_policies.mode=subtitle_only`，不受新番季度和首集自动下载开关限制。`Engine.state`已有状态回调、每轮Engine.cycle的源登记回调、显式SubtitleDelivery.refresh三条路径共用幂等reconcile；进入在追立即检查，后续轮次自动补漏。写事务再次读取范围及watches，显式hold和已有政策不覆盖。它仅启用字幕研究，不冒充首集已评选，不选择primary，不修改普通视频、默认字幕或观看记录。普通追番下载保持登记前的来源规则；新的字幕样本仍须洁净候选、黑名单、保真、真实torrent/manifest及唯一后台提交等检查。

`viewing_workflow.queue_snapshot`除下一未看集与已有工作稿外，还从已有complete分集及complete/linked任务清单发现所有已到料、范围有效且未看的集。较早一集被阻塞时，较晚一集仍能进入准备队列；不据整季集数制造未来生产项，也不因入队变成ready。范围、资料和实际制作能力仍分别检查。

取样前和实际提交前检查watches.finished；后台发现跳过已看集。`subtitle_only`不会重新开启首集选源，但本地第1集仍能沿逐集字幕取料路径处理；改变到wish/trial后停止新的字幕取样。原稿、历史、活动owner和旧accepted保持。请求元数据声明v0→v1，不生成素材/母片/能力证据；已有readiness事实保留。GET status保持只读，返回enrollment_coverage，将missing_eligible_policies和protected_or_unauthorized分开暴露给原调度；实际取料/归档/能力阻塞照实保留。

显式修复命令：

```text
.venv/Scripts/python.exe scripts/repair_subtitle_enrollment.py --apply --show 511177 --episodes 15 16 --output 私有新回执目录
.venv/Scripts/python.exe scripts/repair_subtitle_enrollment.py --apply --all-watching --output 私有新回执目录
```

不带`--apply`只读盘点，仍需`--output`保存报告。apply先保存独立SQLite备份，不自动给其它作品补授权或解除scope_hold。登记只声明请求版本与交付标准，不生成实际素材、亲听、字体或播放验收证据。

## 本次材料与部署交接

私有目录`data/diagnostics/juukishi-subtitle-enrollment-20261016/`保存enrollment-audit.json、before.sqlite3和candidate-manifests.json。已核Mikan候选6037/E15、7323/E16的真实torrent及本地选集；7050/E15作为可进一步比较的保留候选。这只是获取前的身份/manifest核验，未宣称视频完成、全片洁净或字幕内容齐全。

`scripts/deploy_subtitle_enrollment.py`是一次有界本机加载助手，最长4小时。读取实际播放租约、近期web_progress、下载租约、后端idle与本地播放器进程，两次检查后才允许既有supervisor重载一次后端；代码hash改变则停止，不打断播放。不另起Engine，不启动/切换下载器。新后端GET `/api/subtitle-delivery`必须返回preparation_features，才能经既有同源token保护的acquire接口申请上述两份材料；每集一次，有未知响应即保留核查，不自动重试。deployment.json是实际运行状态，不以启动助手当作下载或交付成功。

原调度后续轮次持续核该回执：waiting且有效进程/锁存在时不另起助手；expired_without_interrupting_playback或stopped_code_changed时，仅在旧进程确已退出、锁释放、代码及manifest重新核对后保留旧回执并恢复一次新的有界等待。acquisition_needs_reconcile先核真实任务/回执，不重复申请。不能因播放持续超过4小时便丢失部署交接，也不能绕过播放保护强制重载。本轮不新增定时任务，沿原每15分钟调度及原负责人跟踪。

下载完成后按原宿主对账、不可变原稿归档及当前媒体实帧核查，形成真实readiness，再begin/create/confirm单集唯一负责人。v0实际整集输出并验证统一`/watch?release=...`与隔离续播检查后上架，原writer紧接做v1六项；不得交付独立播放器，不得用新标签充当成品。

## 其它在追作品盘点

本轮检查20部selected/watching作品。除重骑士外，同样缺研究政策且已有独立授权的有：529723《魔法光源股份有限公司 第二季》、530725《死神 千年血战篇-祸进谭-》、558064《女主角？圣女？不，我是杂役女仆（自豪）！》。用户随后明确授权一起补齐：女仆6—12集存在未看普通视频，前两部下一未看分别6、9集且暂无未看complete分集；登记后沿原调度准备，不冒充字幕或洁净母片已就绪。批量修复与保护回执在data/diagnostics/watching-subtitle-enrollment-20261016/。

另4部缺政策的条目同时缺独立授权/映射盘点，保留阻塞，不属于可直接补登记的遗漏。其余作品已有政策；材料缺件、原字幕提取、停写和现有负责人等状态分别保留，不冒充已开工或全部可交付。

## 回归边界

隔离SQLite与FakeQbit验证：季度/新番开关关闭仍登记在追续播；wish→watching即时登记；重复执行不改已有政策和素材证据；删除政策或新增后续complete集可由refresh恢复；已看不创建制作请求；缺授权/映射/盘点、越界、未选、暂停、cleanup_hold和scope_hold均不放行；事务内撤销授权不写入；现有production owner、未知派发与输入绑定保留；只读状态不写DB。加载助手核watching_auto_enrollment功能标记，仍严格等待实际播放空闲。

实际运行本轮广域回归158项通过，补充最终路径回归75项通过，新增首集后台与API两项通过（含重复覆盖，不能相加作独立测试数）。批量回执确认新增3条政策、22条请求元数据，原政策及2个受保护owner不变。原后台同时对既有任务更新updated时间戳，任务hash/status/selection/provider等语义字段保持；不是声称整个生产DB冻结。测试只用隔离DB，真实批量变更是用户本轮授权的登记修复。
