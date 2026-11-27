# 部署与校验命令

| 命令 | 输入与结果 |
| --- | --- |
| scripts/bootstrap-box.sh | 按 deploy/toolchain.env 安装持久工具和 PostgreSQL。重新执行可恢复系统包。`--system-packages` 输出当前发行版的 apt 包名。 |
| scripts/check-toolchain.sh | 检查版本与可执行文件的安装前缀。 |
| scripts/setup-supervisor.sh | 将锁定的 PyYAML 和 Supervisor 安装到 `$FERTILE_ROOT/tools/supervisor`，供 `scripts/healthwatch` 和 `scripts/fertile` 的 Python 控制端使用。详情见 [控制端 Python 依赖](../deploy/linux-server.md#控制端-python-依赖)。 |
| scripts/fertile build [--ref SHA] | 从指定总仓提交的 gitlink 构建 finance、web、前端，校验产物并原子切换 current。 |
| scripts/fertile build --stage-only [--ref SHA] | 复用构建与制品校验，将下一发布保存在 `releases/` 待选择。 |
| scripts/fertile container | 以 tini 管理前台 supervisord，作为容器入口加载同一进程清单。 |
| scripts/fertile up / ensure / down | supervisord 依序启动、重启后恢复、逆序优雅停止服务。 |
| scripts/fertile install-systemd --root PATH --user USER --group GROUP | 管理员为选定发布安装核心服务 unit，启用 `fertile.target` 开机启动。 |
| scripts/fertile uninstall-systemd --root PATH | 管理员停止并停用该实例的 `fertile.target`，移除本仓库安装的 unit。 |
| scripts/fertile verify-systemd | 渲染并静态检查样例 unit。若有 systemd-analyze 则执行 verify。 |
| scripts/fertile status --json | 读取已安装 systemd 实例或 supervisord 的进程状态，以及 HTTP live / ready 结果。 |
| scripts/fertile restart SERVICE / logs SERVICE | 操作当前实例所属守护器，分别重启服务或读取近 100 行日志。systemd 模式日志来自 journal。 |
| scripts/fertile hot-upgrade SERVICE SHA --hook PATH | 验证兼容性，完成指定进程的候选就绪或停写交接，记录服务发布指针。`--migrate` 供隔离演练，`--rollback` 仅用于无新增迁移且新版未写过旧版目标库的兼容回退。[热回退条件](../deploy/hot-upgrade.md#升级与兼容回退) |
| scripts/fertile hot-upgrade-status [--json] / hot-upgrade-reconcile ID --hook PATH | 读取热升级事务或在核对实际进程、租约和发布指针后确认中断结果。详见[热升级与本机接口](../deploy/hot-upgrade.md)。 |
| scripts/fertile data-root | 输出当前环境与 `config/runtime.env` 解析出的 finance 数据根。 |
| scripts/healthwatch | 采样本机健康状态、记录告警并调度备份。未来生产服务器可通过 `FERTILE_PUBLIC_ORIGIN` 或巡检覆盖文件显式启用 HTTPS 就绪探测。详见 [巡检与告警](../ops/monitoring.md)。 |
| scripts/fertile rollback SHA | 校验既有版本并切换，重新加载受影响服务。共享目标仅适用于无新增迁移、数据兼容且新版未写过目标库的回退。 |
| scripts/fertile rollback --switch-only SHA | supervisor 已停时，校验既有版本并切换 `current`。含新增迁移的发布先恢复迁移前备份至 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，随后用这一对目标运行旧发布。[隔离恢复流程](../ops/ruler-runbook.md#从升级前快照回滚并切回新版) |
| scripts/backup.sh | 将活动库、队列、catalog 和公开配置备份到持久目录。 |
| python3 scripts/backup_remote.py full / hourly | 通过 `rclone crypt` 发布独立日全量或基于已验证父点的小时增量。 |
| python3 scripts/backup_remote.py status [--cached] | 输出最后可恢复到的时间、点 ID、年龄及告警状态。`--cached` 使用本机上次观测。 |
| python3 scripts/backup_remote.py restore POINT | `POINT` 使用确切点 ID 或 `latest`。`--snapshot-to PATH` 重建独立快照。`--target-data-root PATH --target-database NAME` 执行正式隔离恢复。 |
| python3 scripts/backup_remote.py prune | 在有近期成功恢复凭据时按日、周和父链规则清理远端。详见 [异地备份](../ops/backup.md#私人-google-drive-加密备份)。 |
| scripts/restore.sh SNAPSHOT --target-data-root PATH --target-database NAME | 校验快照并恢复到显式目标。`--dry-run` 只读校验，在指定目标时也检查占用。`--overwrite` 在正式恢复时允许覆盖，在 dry-run 时警告非空目标的处理方式。`--target-research-root` 单独指定研究 catalog。参见 [备份操作](../ops/backup.md)。 |
| scripts/check-submodules.sh [--ref BRANCH] | 检查三个 gitlink、干净的递归工作树、远端分支上已推送的提交，默认 main。 |
| scripts/contract-diff.sh | 从两个生产者代码导出 OpenAPI，校验 finance 流契约，并重跑两端客户端生成检查。 |
| scripts/release.sh --dry-run | 从 Git 和 GitHub 读取版本、包含的提交与 PR、关闭 issue，向标准输出写入预览。 |

服务使用 [部署清单](../deploy/service.yaml)。systemd 的安装和切换步骤见 [普通服务器部署指南](../deploy/linux-server.md)，压测盒子操作、备份与故障处理见 [Ruler 手册](../ops/ruler-runbook.md)，系统版本规则见 [发版文档](../docs/release.md)。

`data-guard.py` 提供 Git 索引、push 记录、CI 提交范围、私有目录和公开报告检查。钩子安装与共享规则见 [四仓数据发布政策](../docs/data-policy.md)。

接口不变的祖先升级需通过实际导出内容的比较。T3a 的 W6 锁定 finance `fc5e153`，U6 锁定 web `6e5fd80`。契约检查核对两个生产者的导出及下游客户端。[契约验证流程](../contracts/README.md#验证)

本机压力测试与部署复验使用 `tests/e2e-smoke.sh local --paper-only`，其中覆盖根路径 `/NOTICE` 和第三方许可证路径。手工核对响应与静态制品的方法见 [第三方许可证复验](../ops/ruler-runbook.md#第三方许可证复验)。公网巡检按默认配置跳过。

## 本机历史验收

官方四窗口验收使用 finance 的一次性入口 `fertile_finance.isolated_historical_job`。它从独立数据根和研究根启动专用 API 与 worker，并向空闲的环回端口提交。`tests/e2e-smoke.sh local` 的研究回测模式、`/backtests` 页面上的提交和经 8090 的 POST 都会写入现网队列，冒烟脚本还会先提交示例回测。[隔离验收官方四窗口历史作业](../ops/ruler-runbook.md#6-隔离验收官方四窗口历史作业)

本机验收期间禁止修改 `runtime.env` 或 `env.sh`、重指 live API 或 worker、重启 live 服务，以及对 `data-paper` 或现网库 `web` 执行正式恢复。禁止运行 `scripts/fertile up`、`ensure`、`ensure-up`、`down` 或 `rollback`，这些命令会管理现网服务，并可能拉起 healthwatch 或调用 `ensure`。
