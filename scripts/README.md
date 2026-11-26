# 部署与校验命令

| 命令 | 输入与结果 |
| --- | --- |
| scripts/bootstrap-box.sh | 按 deploy/toolchain.env 安装持久工具和 PostgreSQL。重新执行可恢复系统包。`--system-packages` 输出当前发行版的 apt 包名。 |
| scripts/check-toolchain.sh | 检查版本与可执行文件的安装前缀。 |
| scripts/setup-supervisor.sh | 将锁定的 PyYAML 和 Supervisor 安装到 `$FERTILE_ROOT/tools/supervisor`，供 `scripts/healthwatch` 和 `scripts/fertile` 的 Python 控制端使用。详情见 [控制端 Python 依赖](../deploy/linux-server.md#控制端-python-依赖)。 |
| scripts/fertile build [--ref SHA] | 从指定总仓提交的 gitlink 构建 finance、web、前端，校验产物并原子切换 current。 |
| scripts/fertile up / ensure / down | supervisord 依序启动、重启后恢复、逆序优雅停止服务。 |
| scripts/fertile install-systemd --root PATH --user USER --group GROUP | 管理员为选定发布安装核心服务 unit，启用 `fertile.target` 开机启动。 |
| scripts/fertile uninstall-systemd --root PATH | 管理员停止并停用该实例的 `fertile.target`，移除本仓库安装的 unit。 |
| scripts/fertile verify-systemd | 渲染并静态检查样例 unit。若有 systemd-analyze 则执行 verify。 |
| scripts/fertile status --json | 读取已安装 systemd 实例或 supervisord 的进程状态，以及 HTTP live / ready 结果。 |
| scripts/fertile restart SERVICE / logs SERVICE | 操作当前实例所属守护器，分别重启服务或读取近 100 行日志。systemd 模式日志来自 journal。 |
| scripts/fertile data-root | 输出当前环境与 `config/runtime.env` 解析出的 finance 数据根。 |
| scripts/healthwatch | 采样本机健康状态、记录告警并调度备份。未来生产服务器可通过 `FERTILE_PUBLIC_ORIGIN` 或巡检覆盖文件显式启用 HTTPS 就绪探测。详见 [巡检与告警](../ops/monitoring.md)。 |
| scripts/fertile rollback SHA | 校验既有版本并切换，重新加载受影响服务。 |
| scripts/fertile rollback --switch-only SHA | supervisor 已停时，校验既有版本并切换 `current`，由后续 `ensure-up` 启动。 |
| scripts/backup.sh | 将活动库、队列、catalog 和公开配置备份到持久目录。 |
| scripts/restore.sh SNAPSHOT --target-data-root PATH --target-database NAME | 校验快照并恢复到显式目标。`--dry-run` 只读校验，在指定目标时也检查占用。`--overwrite` 在正式恢复时允许覆盖，在 dry-run 时警告非空目标的处理方式。`--target-research-root` 单独指定研究 catalog。参见 [备份操作](../ops/backup.md)。 |
| scripts/check-submodules.sh [--ref BRANCH] | 检查三个 gitlink、干净的递归工作树、远端分支上已推送的提交，默认 main。 |
| scripts/contract-diff.sh | 从两个生产者代码导出 OpenAPI，并重跑两端客户端生成检查。 |
| scripts/release.sh --dry-run | 从 Git 和 GitHub 读取版本、包含的提交与 PR、关闭 issue，向标准输出写入预览。 |

服务使用 [部署清单](../deploy/service.yaml)。systemd 的安装和切换步骤见 [普通服务器部署指南](../deploy/linux-server.md)，压测盒子操作、备份与故障处理见 [Ruler 手册](../ops/ruler-runbook.md)，系统版本规则见 [发版文档](../docs/release.md)。

本机压力测试与部署复验使用 `tests/e2e-smoke.sh local --paper-only`，公网巡检按默认配置跳过。

## 本机历史验收

官方四窗口验收使用 finance 的一次性入口 `fertile_finance.isolated_historical_job`。它从独立数据根和研究根启动专用 API 与 worker，并向空闲的环回端口提交。`tests/e2e-smoke.sh local` 的研究回测模式、`/backtests` 页面上的提交和经 8090 的 POST 都会写入现网队列，冒烟脚本还会先提交示例回测。[隔离验收官方四窗口历史作业](../ops/ruler-runbook.md#6-隔离验收官方四窗口历史作业)

本机验收期间禁止修改 `runtime.env` 或 `env.sh`、重指 live API 或 worker、重启 live 服务，以及对 `data-paper` 或现网库 `web` 执行正式恢复。禁止运行 `scripts/fertile up`、`ensure`、`ensure-up`、`down` 或 `rollback`，这些命令会管理现网服务，并可能拉起 healthwatch 或调用 `ensure`。
