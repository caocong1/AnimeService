# 字幕制作的生肉与软字幕片源

2026-10-11用户要求接入Nyaa和TokyoTosho，以无翻译字幕或可关闭字幕的画面作为自行制作字幕的基底。

2026-10-11 18:50 +0800补充授权：Netflix、Amazon/Prime Video类作品也查BT4G和1337x。`general_indexes_enabled=true`已部署；仍仅在上述个人字幕研究边界内查源。当前14部有目录平台信息或经身份/季集过滤的NF/AMZN发布线索，增加56条公开搜索来源（每站28）。平台线索只触发补充搜索，不证明独播，不改变其它影视或季度范围。

生肉是未加翻译字幕的版本；熟肉带翻译字幕。硬字幕已压入画面，无法关闭；内封/外挂的软字幕可关闭，原生日文或中文软字幕也可保留作为校订依据。发布者名字带`raws`、文件扩展名为MKV，都不证明画面无硬字幕。

## 范围与发现

`native_indexes_enabled=true`仅给现有个人想看且独立授权、盘点、映射和首集边界有效的字幕研究作品补公开RSS。保持源开关与URL归属，不重新授权、不改个人状态，也不补旧季度或删除/替换现有文件。季度普通下载范围继续独立。

- Nyaa使用已核实的`https://nyaa.si/`，动画搜索RSS`page=rss&c=1_0`，覆盖生肉分类及明确声明MultiSub/软字幕的发布。`nyaa.eu`代理不接入。
- TokyoTosho使用`https://www.tokyotosho.info/rss.php`，`filter=7`为Raws、`filter=1`为Anime。种子链接经常指向Nyaa，保留Tokyo详情与Nyaa种子地址两份来源。
  该站有三个官方域名`www.tokyotosho.info`、`www.tokyotosho.se`、`www.tokyo-tosho.net`：来源表只记录`.info`规范地址，抓取时按站点健康轮换域名（某域名返回站点级错误后下一次换下一个域名），候选详情链接接受这三个域名。
- TokyoTosho 对没有匹配的搜索词返回17字节的`</channel> </rss>`残片（Content-Type为text/html）。这是合法空结果，记为`本次RSS无条目`，不计失败；2026-10 之前它被当成"RSS格式无效"并以300秒上限不断重试，是该站错误事件的主要来源。
- 站点级健康（`kv site_health_<站点>`）：同一站点族的全部来源共享冷却（首次60秒，指数增长，上限1小时，成功即清零；服务器`Retry-After`更长时按其值）与最小请求间隔（tokyotosho 10秒、nyaa 5秒，可用`kv site_min_interval_seconds`按站点覆盖；蜜柑等不限制）。被间隔推迟的来源按排队位置错开下次时间（10、20、30秒…），不计失败，手动检查同样受限。站点级故障只包括连接失败、HTTP 403/408/425/429/5xx 与带 Cloudflare 特征的验证页；对这些答复`Web.fetch`不再盲目重试。返回普通HTML或feed格式错误只影响该来源，来源自身退避从60秒翻倍到6小时，成功即清零。健康记录保留`stats`（polls/deferred/success/empty/failed_site/failed_source）与最近错误时间，便于上线前后比较。同一来源连续相同错误只在第1、5、20、100次及每100次写事件；蜜柑/动漫花园历史页解析不到条目时只在错误内容变化时写一次警告。`bt4g.org`/`bt4gprx.com` 与 `1337x.to`/`www.1337x.to` 各视为同一站点共享健康记录。
- 搜索词来自既有核验别名和原名，只供发现；标题和实际torrent文件仍按完整身份与明确季号核验。续作搜索缺季号不入候选；不借RSS关联推断同季。
- RSS通常仅返回有限的最新条目；Nyaa当前75、Tokyo最多150，持续轮询后持久保存发现，不宣称全站历史已覆盖。

## 通用BT补充

- BT4G的`bt4g.org`当前跳转`bt4gprx.com`。HTML搜索遇Cloudflare，但本机已核实公开RSS：`/search?q=…&orderby=time&category=movie&p=1&page=rss`。`movie`是该站视频大类，仍需逐条核作品与实际单集。RSS一次15条，保存GUID详情、磁力hash和发布时间；去掉第三方tracker/dn参数，仅保存公开hash。每小时轮询，不冒充全站历史盘点。
- 1337x使用`1337x.to/search/…/1/`，解析搜索表与详情中的磁力、明确软字幕声明、Raw分类81。标题省略或不明字幕模式不猜；详情最多6页/查询。主站、本次核查的官方列出备用域名、既有本机HTTP代理及独立TLS客户端均未解除Cloudflare拦截。适配已部署，当前生产搜索HTTP403、尚无真实1337x候选/下载成功；按站点共享1小时退避，避免每部作品重复失败拖慢队列，不启动浏览器或桌面。
- BT4G仅标Movie、发布者含raws或MKV不能判生肉。可以采用标题明确MultiSub/softsubs/无字幕，或同hash的已有合格索引分类/声明作交叉证据；记录对应candidate/hash，仍只是获取声明。硬字幕声明优先排除。1337x详情声明保存文本hash。通用标题常将发布组移到后缀，当前发布者黑名单别名在后缀也排除；实际种子文件继续核黑名单和AI保真。
- 磁力不直接投递qB。先读本机已有hash的torrent缓存、同hash的公开种子链接，再按hash查询Nyaa，最后尝试1337x上游适配支持的`itorrents.net/torrent/HASH.torrent`。每条路径必须bdecode并核infohash，随后沿实际身份/明确续作季号/单集选择/磁盘与双下载器去重门禁。缺元数据时保留原有候选/重试流程，未提交下载器；不会创建一个下载正片后才核身份的磁力任务。

本机实核：乱马搜索纳入7个合格候选、战车椅子8个、JOJO本次没有合格候选；实际读取并核验3个乱马torrent的hash、文件清单、S03E02。此脚本只发现/读manifest，新增下载器提交0；后续由原后台队列处理，跨站同hash共用既有任务。证据[verification.json](general-indexes-verification-20261011/verification.json)，部署前后个人/观看/提交不确定快照一致见[deployment.json](general-indexes-verification-20261011/deployment.json)。正常后台发现量会继续变化。

格式核对依据：[SearXNG原项目BT4G公开RSS适配](https://github.com/searxng/searxng/blob/master/searx/engines/bt4g.py)、[Jackett原项目1337x搜索/磁力/种子链接定义](https://github.com/Jackett/Jackett/blob/master/src/Jackett.Common/Definitions/1337x.yml)。本次临时DB/假下载器103项及补充74项检查通过，批次重叠不相加；生产发现与manifest读取独立记录。

`candidates.source_metadata`保存索引、分类、声明字幕模式、声明infohash和做种数。RSS GUID是详情页，link是种子下载地址。取得torrent后核对infohash，并沿原黑名单/保真、实际季集与逐文件选择、磁盘、双下载器同hash及不确定提交门禁。

## 站点请求的验收边界

TokyoTosho 无匹配时返回的 `</channel> </rss>` 残片仅在该站 RSS 路径解释为合法空结果；空正文、其它站点残片与未知 HTML 仍记录明确错误。522 等全部 5xx 属于站点故障；403、429 与明确验证页共享冷却，但不轮换域名绕开限制。`Retry-After` 支持秒数和 HTTP 日期，不截短服务器要求的等待。

RSS、详情、种子请求及重定向共用持久请求预算。短 SQLite 事务只预留请求名额，不在事务内联网或等待；延迟的重定向保存有上限和有效期的继续位置，下一时隙继续，既不连发，也不反复请求第一跳。规范来源 URL、作品归属、种子 hash 及下载提交门禁保持原职责。

`site_health_<site>.stats.http_attempts` 记录实际发送名额，`polls` 记录已结束的来源轮询，等于 `success + empty + failed_site + failed_source`；`deferred` 单列，不能当失败。候选入库异常不得同时计成功。验收以当前 eligible 来源为分母，保留未授权或停用来源，不能为清零错误强制刷新它们。134 条来源在 10 秒间隔下仅发送开始时间就需要约 22 分钟，实际轮转还受引擎周期及其它工作影响，22 分钟不是保证时限。

## 下载与验收

策略拒绝使用 `Refusal` 保留可执行的门禁原文，未确认或已接收的提交继续对账，不因拒绝缓存重新投递。未接收 intent 的输入不变时，复查间隔为 10 分钟起、指数增长至 6 小时；候选失败从 5 分钟起至 6 小时。显式检查按请求标识仅越过一次，避免一轮未结束期间反复穿透退避。

拒绝输入摘要包括下载/字幕开关、来源保真与黑名单、观看工作流、作品政策与总集数、任务用途及外部接收标识、相关候选的来源证据和字幕取样参考。证据变化立即重新判断。完成任务每小时复核文件，下载器错误、进度变化、门禁输入变化或新的显式检查立即复核；仍保留文件丢失检查。`task_failure_*`、`candidate_retry_*`、`task_verified_*` 与 `task_verified_inputs_*` 为可留存的缓存，不替代任务与提交事实，不需要表结构迁移。

`subtitle_clean_media_preferred=true`只影响个人字幕制作范围的新获取：优先生肉，其次明确软字幕；未明确模式和硬字幕不再新投递。已有完成、在途及提交不确定记录保留并继续对账。已完成字幕稿与媒体不覆盖。

首集仍走`source_sample`，后续实际单集走`subtitle_sample`，沿每集12研究族/20GiB预算和本机后台qB。跨站同hash不重复提交。软字幕声明仅作取样依据，实际轨道及画面仍须核验；默认字幕仍要求整集语义、原声/时轴、片内文字和实际播放接受。

2026-10-11按用户明确要求修复qB：确认无qB进程/4870监听后，一次性隐藏恢复既有`D:/MediaService/qbit-profile`实例。未改默认实例、凭据、全局目录或其它任务；平时不自动启动/切换实例的政策继续。运行中的现有qB由自身继续下载，停止后不因查源失败自行再启动。

验证使用临时数据库、假下载器及模拟RSS；实际下载属于用户已授权的运行任务，不能拿生产提交做测试。上游RSS格式依据[Nyaa原项目模板](https://github.com/nyaadevs/nyaa/blob/master/nyaa/templates/rss.xml)，qB接口依据[qBittorrent官方API文档](https://github.com/qbittorrent/wiki/blob/master/WebUI-API-(qBittorrent-5.0).md)。
