# 贡献说明

先读 README 和 docs/DEVELOPMENT.md，使用独立开发数据库。项目中的本机 AGENTS.md、REQUIREMENTS.md、DECISIONS.md、PROGRESS.md、HANDOFF.md 是部署记录，不随仓库发布；如果你的工作目录已有这些文件，先读并遵守本机边界。

普通贡献流程：从 main 创建分支，修改和验证，提交分支并发起 Pull Request。PR 写清问题、最终行为和验证结果；界面修改附使用虚构数据的截图，覆盖桌面、窄屏、空态与错误态。

```sh
git switch -c improve-ui
# 编辑并测试
git add static
git commit -m "Improve library navigation"
git push -u origin improve-ui
```

没有仓库写权限时先 fork。合并后其他电脑用 `git pull --ff-only` 更新；有本机修改时先检查和保存，不使用强制重置覆盖工作。部署数据不走 Git。

必须保留的业务边界：

- 追番意愿、下载授权、已下载与已看分别管理，资料同步不能恢复弃番或下载授权。
- 迅雷优先，qB 备用必须经过 download_policy.py；不能自动启动或切换 qB 实例来绕过门禁。
- qB 任务必须同时匹配项目台账、分类 AnimeService 和标签 AnimeService-v1；迅雷必须同时核对 hash、TaskId、目录和文件清单。
- 暂搁、弃番、看完和 cleanup_hold 不得自动恢复；打开播放器、退出位置或快进不能作为看完。
- 已核验的弹弹play LastWatched 自动计入 watches.finished，手动修正优先；不能退回仅显示历史列表的旧行为。
- 旧媒体只读关联；删除必须由用户在页面确认具体文件清单，不递归清理媒体目录。
- 测试用临时库和假下载器；不打印或提交密码、Cookie、token、私人 RSS 或真实片库数据。

UI 改动保持 API 合约，涉及业务行为时一并更新隔离测试。不要把第三方依赖生成目录或本机设计截图加入提交。
