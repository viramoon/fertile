# 部署与校验命令

| 命令 | 输入与结果 |
| --- | --- |
| scripts/bootstrap-box.sh | 按 deploy/toolchain.env 安装持久工具和 PostgreSQL；重新执行可恢复系统包。 |
| scripts/check-toolchain.sh | 检查版本与可执行文件的安装前缀。 |
| scripts/fertile build [--ref SHA] | 从指定总仓提交的 gitlink 构建 finance、web、前端，校验产物并原子切换 current。 |
| scripts/fertile up / ensure / down | 依序启动、重启后恢复、逆序优雅停止服务。 |
| scripts/fertile status --json | 输出服务启用状态、进程以及可用的 live / ready 结果。 |
| scripts/fertile data-root | 输出当前环境与 `config/runtime.env` 解析出的 finance 数据根。 |
| scripts/fertile rollback SHA | 校验既有版本并切换，重新加载受影响服务。 |
| scripts/fertile rollback --switch-only SHA | supervisor 已停时，校验既有版本并切换 `current`，由后续 `ensure-up` 启动。 |
| scripts/backup.sh | 将活动库、队列、catalog 和公开配置备份到持久目录。 |
| scripts/restore.sh SNAPSHOT --target-data-root PATH --target-database NAME | 校验快照并恢复到显式目标。`--dry-run` 只校验，`--overwrite` 允许覆盖非空目标，`--target-research-root` 单独指定研究 catalog。参见 [备份操作](../ops/backup.md)。 |
| scripts/check-submodules.sh [--ref BRANCH] | 检查三个 gitlink、干净的递归工作树、远端分支上已推送的提交，默认 main。 |
| scripts/contract-diff.sh | 从两个生产者代码导出 OpenAPI，并重跑两端客户端生成检查。 |
| scripts/release.sh --dry-run | 从 Git 和 GitHub 读取版本、包含的提交与 PR、关闭 issue，向标准输出写入预览。 |

服务使用 [部署清单](../deploy/service.yaml)。操作步骤、备份与故障处理见 [Ruler 手册](../ops/ruler-runbook.md)，系统版本规则见 [发版文档](../docs/release.md)。
