# 网页播放与弹幕：2026-09-27实测

结论：浏览器播放本机动画并叠加多平台弹幕可实现，已部署预览入口 http://127.0.0.1:4871/watch 。目前不是弹弹play的完整替代：巴哈姆特直连失败；MKV/HEVC/内嵌字幕未实现通用转码与字幕渲染，保留桌面播放器备选。

## 调研项目与取舍

| 项目 | 能力与本次决定 |
|---|---|
| [kaedei/dandanplay-libraryindex](https://github.com/kaedei/dandanplay-libraryindex) | 官方MIT项目，远程API可提供视频Range串流及最新弹幕XML。最适合保留既有PC弹幕插件；本机远程接口未启用，未更改其凭据、权限或中断当前播放器。 |
| [wiidede/dandanplay-vi](https://github.com/wiidede/dandanplay-vi) | MIT网页播放器，文件hash匹配开放弹幕网络，提供ArtPlayer等播放选项。可参考UI/匹配流程；不整体引入另一套前端。 |
| [canmo101/danmuapi](https://github.com/canmo101/danmuapi) | 多平台搜索/剧集/弹幕，支持Node原生部署。独立进程采用此能力，固定提交28673ac764485b0f37966779bc3b14e2b97d31aa；实际LICENSE为AGPL-3.0（package.json标ISC与其不一致，以仓库许可证记录）。 |
| [ArtPlayer](https://github.com/zhw2590582/ArtPlayer) | 本地vendor固定ArtPlayer5.4.0、弹幕插件5.3.0。负责播放、全屏、倍速及弹幕渲染；不负责从视频平台提取视频。 |

## 弹弹play的原理

[官方说明](https://doc.dandanplay.com/faq/load-danmaku.html)：文件特征与文件信息匹配到独立episodeId，获取弹弹play弹幕库；按条件获取关联第三方网址，用PC插件解析或代理缓存获取，再合并去重。并非每次实时请求所有平台；不同版本还需要校正时间轴。

[开放网络](https://doc.dandanplay.com/open/)要求开发者申请AppId/AppSecret；本次未提取其他应用密钥、未新开账号、未开通付费服务。采用本机聚合服务独立获取公开弹幕。官方远程API的comment和stream说明见 [API.md](https://github.com/kaedei/dandanplay-libraryindex/blob/master/api/API.md)。

## 实测证据与限制

- 《葬送的芙莉莲》2023第一季第1集：B站6633条、爱奇艺3925条、优酷1848条、腾讯6343条，四站请求均HTTP200且有非空comments；原始响应在research目录。只下载弹幕和公开元数据，没有提交媒体下载。
- 巴哈姆特官方搜索接口请求重试后未取得结果，未使用代理绕过或他人凭据；不能宣称五平台均可用。
- 搜索“第二季”时源站仍可能返回第一季条目，因此网页明确要求核对版本/季集，不自动接受首条匹配；目前需手工选择每集来源。
- 来源选择绑定已返回的公开平台URL，不持久使用聚合服务进程内自增episodeId，防止重启/缓存重建后串集。重启后需重新选择来源。
- 本机旧媒体1317个文件：MKV962、MP4355。检查MP4样本为H.264/AAC，实际浏览器载入画面和23:40时长，Range测试206通过；不能据此声称所有MKV/HEVC/ASS可用。
- 网页只保存播放位置及实际播放活动，不将打开、拖动或结束事件自动计作看完。整集状态仍可在原详情手动修正。

## 本地部署与接续

主服务4871；独立danmuapi只绑定127.0.0.1:4872。禁用上游自带5321通用代理监听，不开放防火墙和公网。源码与LICENSE保存在vendor/danmuapi，两个本机适配改动有git diff；上游默认配置不含本机凭据。

主后台每30秒检查适配进程并在必要时重启。Stop-AnimeService.ps1同时停止该适配。安装重建执行 `.venv\Scripts\python.exe scripts\setup_web.py`，固定源码版本、npm锁文件、禁止依赖安装脚本，然后复制播放器静态文件。无需Docker。

后续优先完成：官方PC桥接的最新聚合弹幕验证、巴哈姆特网络问题定位、MKV/HEVC兼容转码/字幕、基于明确作品季集映射的自动弹幕选择。当前仍保留预览标识，不默认强制替换弹弹play。


## 2026-09-27：巴哈姆特经本机代理接通

用户提示后只读核实Windows系统代理已启用127.0.0.1:7897。对同一巴哈姆特搜索API实测：直连16.03秒ConnectTimeout；显式HTTP代理0.70秒HTTP200，返回芙莉莲一/二季。Node适配器经HTTPS CONNECT后第二季分集列表10集、首集连续编号29/videoSn47221及3001条原始弹幕均HTTP200。一次Python请求曾403/一次TLS EOF；最终Node链路成功，不能保证代理永远可用。

修复：integrations/bahamut-proxy.js仅为api.gamer.com.tw使用现有本机HTTP代理，配置vendor/danmuapi/config/.env中的ANIMESERVICE_BAHAMUT_PROXY=http://127.0.0.1:7897。不更改系统代理、不切换节点、不关闭TLS验证、不开放5321通用代理。其他平台不受影响。scripts/patch_danmu_proxy.py可重放补丁，setup_web.py已调用；上游固定版本不变。代理地址变更后修改本项目env并重启弹幕适配器，关闭代理时该来源可能超时；依然保留弹弹play。

webplayer.py将上游标有【bahamut】的数字videoSn固定绑定到https://ani.gamer.com.tw/animeVideo.php?sn=编号，拒绝未标源的数字及其他路径。网页手动核对季度和集数，再加载；平台第29集是该作品第二季第1集，勿按本地S02E29匹配。

验证：30项Python隔离测试通过，1项Node代理范围测试通过。研究原始结果在docs/bahamut-proxy-audit.json、docs/research/bahamut-adapter-*.json。此次未改变迅雷优先/自动提交暂停策略，未操作媒体下载或清理。

最终页面验收：发现上游getCommentByUrl缺少巴哈姆特路由（只有按临时ID的接口支持），已在patch_danmu_proxy.py补上固定网址识别，避免误发第三方兜底。经本项目API第29集返回2698条规范化/去重弹幕；浏览器为旧本机第35集加载匹配巴哈姆特2079条，视频1920×1080、1440秒、paused=true，未标看完。截图docs/bahamut-web-proof.png，完整证据docs/bahamut-web-smoke.json。
