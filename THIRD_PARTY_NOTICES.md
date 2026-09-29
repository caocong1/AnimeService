# 第三方组件

本仓库的 MIT 许可证仅覆盖项目原创代码，以下组件保留各自许可证：

| 组件 | 来源 / 版本 | 许可 |
| --- | --- | --- |
| ArtPlayer | https://github.com/zhw2590582/ArtPlayer · 5.4.0 | MIT |
| artplayer-plugin-danmuku | 同上 · 5.3.0 | MIT |
| danmuapi | https://github.com/canmo101/danmuapi · 28673ac764485b0f37966779bc3b14e2b97d31aa | AGPL-3.0，见上游 LICENSE |

播放器构建产物和 danmuapi checkout 不随仓库提交。`scripts/setup_web.py` 下载它们，并对独立弹幕服务应用仓库内可见的回环监听、关闭通用代理和巴哈姆特代理补丁。上游 LICENSE 保留在 checkout；分发或部署修改后的上游组件时须遵守其许可证，不得把它标为本项目 MIT 代码。

Python 依赖列在 requirements.txt，npm 依赖版本及完整依赖树列在 package-lock.json，各包的许可见安装包和上游仓库。海报、作品资料、弹幕和媒体不属于本项目授权的内容；本仓库不包含这些数据。
