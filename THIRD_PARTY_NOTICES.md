# 第三方组件

本仓库的 MIT 许可证仅覆盖项目原创代码，以下组件保留各自许可证：

| 组件 | 来源 / 版本 | 许可 |
| --- | --- | --- |
| ArtPlayer | https://github.com/zhw2590582/ArtPlayer · 5.4.0 | MIT |
| artplayer-plugin-danmuku | 同上 · 5.3.0 | MIT |
| libass-wasm / SubtitlesOctopus | https://github.com/libass/JavascriptSubtitlesOctopus · 4.1.0 | libass LGPL-2.1-or-later 等组合许可；完整清单位于安装目录 COPYRIGHT |
| Noto Sans CJK SC | https://github.com/notofonts/noto-cjk · Sans2.004 | SIL Open Font License 1.1 |
| danmuapi | https://github.com/canmo101/danmuapi · 28673ac764485b0f37966779bc3b14e2b97d31aa | AGPL-3.0，见上游 LICENSE |
| Chromaprint / fpcalc | https://github.com/acoustid/chromaprint · 1.6.1 | LGPL-2.1-or-later；官方 Windows 二进制及其依赖许可随工具保留 |
| yt-dlp | https://github.com/yt-dlp/yt-dlp · 2026.08.19 | Unlicense，依赖保留各自许可 |
| NumPy | https://github.com/numpy/numpy · 2.4.3 | BSD-3-Clause，二进制依赖许可见安装包 |

播放器构建产物和 danmuapi checkout 不随仓库提交。`scripts/setup_web.py` 下载它们，并对独立弹幕服务应用仓库内可见的回环监听、关闭通用代理和巴哈姆特代理补丁。上游 LICENSE 保留在 checkout；分发或部署修改后的上游组件时须遵守其许可证，不得把它标为本项目 MIT 代码。

Python 依赖列在 requirements.txt，npm 依赖版本及完整依赖树列在 package-lock.json，各包的许可见安装包和上游仓库。海报、作品资料、弹幕和媒体不属于本项目授权的内容；本仓库不包含这些数据。

ASS 渲染器以独立、未修改的 JS/WASM 文件安装到 `static/vendor/libass`，保留 COPYRIGHT。字体回退文件及 OFL 许可由 `scripts/setup_subtitles.py` 安装，下载使用固定版本与 SHA-256 校验。媒体内嵌字体仅为当前注册媒体的播放按需读取，不随仓库分发。
