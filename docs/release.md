# fertile 发版与集成规则

只有 fertile 对外带版本。总仓库 gitlink、同一次构建的静态文件和两个后端产物共同决定一次部署。发布部署与回滚由 Maas · Ruler 按已验证的 fertile tag 操作。当前 ha-runthrough 使用明确提交联调，版本维持 0.0.0。

## 验证固定提交

先将子仓库提交合并到各自的远端 main，再更新总仓的三个 gitlink。fertile 的 ha-runthrough 分支与主线均按子仓库 main 验证锁定提交。

~~~sh
git submodule update --init --recursive
scripts/check-submodules.sh
scripts/contract-diff.sh
scripts/release.sh --dry-run
~~~

默认运行 `scripts/check-submodules.sh` 会从 GitHub 查询各 main，验证锁定的提交属于该分支历史。脚本也会检查嵌套 Nautilus checkout 与子模块工作树。托管 CI 递归读取私仓需提供 Actions secret `FERTILE_SUBMODULES_TOKEN`，最小权限为四仓库 Contents 只读。CI 使用同一权限补取本地缺失的契约来源提交，供[契约检查](../contracts/README.md)核对。

## 发布预览

在含有完整历史和发布 tag 的 checkout 运行 dry-run，并让 gh 使用能读取这些私仓 PR 与 issue 的账号。预览读取 HEAD:VERSION、三个 gitlink 和最近可达的系统版本标签，列出新增提交关联的目标为 main 的 PR、合并状态和 GitHub 记录的关闭 issue。首次发布使用各锁定提交的全部祖先。标准输出可重定向到审阅文件；查询失败会以非零退出。

正式发布另行授权。发布变更需在同一 PR 固定 VERSION、指针和脚本生成的 RELEASES.md，经集成验证再创建不可移动的系统标签 vX.Y.Z 和子模块内部标签 fertile-vX.Y.Z。当前命令仅接受 --dry-run。数据库与 catalog 格式兼容以及对应备份是回滚的前提，详见 [Ruler 手册](../ops/ruler-runbook.md)。
