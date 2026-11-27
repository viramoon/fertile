# fertile

fertile 将 finance 的行情与回测、web 的 HTTP 网关和前端看板部署在同一台 Linux 主机上。浏览器访问 web，web 经 HTTP 调用 finance。finance 将行情写入 Nautilus Parquet catalog，worker 从持久队列执行回测。PostgreSQL 保存 web 的 app 和 blog 数据。券商接入范围为 IBKR paper 只读行情。

## 如何使用

Ubuntu 24.04 和 Debian 12（x86_64）的普通服务器按[部署指南](deploy/linux-server.md)准备运行账户、构建并安装 systemd 服务。无 systemd 的主机可使用 supervisord，容器以 tini 包装前台 supervisord。两种守护器共用一份进程清单，本地压测盒子的部署与恢复由 Ruler 管理。

总仓库 `fertile` 及其子模块 `finance`、`web`、`frontend` 均为公开仓库。开发联调可在初始化子模块前检出指定提交。以下命令适用于新建的独立开发实例。

~~~sh
git clone https://github.com/viramoon/fertile.git
cd fertile
# 如需固定版本，在初始化子模块前 checkout 所选提交或发布 tag。
git submodule update --init --recursive
export FERTILE_ROOT="$HOME/fertile-dev"
scripts/bootstrap-box.sh
source "$FERTILE_ROOT/env.sh"
scripts/fertile build
~~~

以下示例选择 replay 行情并由 supervisord 启动。paper 部署先按 [Gateway 操作说明](docs/ibkr-gateway.md) 安装并配置只读登录，使用与 replay 独立的数据目录。

~~~sh
mkdir -p "$FERTILE_ROOT/config"
# 仅在新测试实例上创建；已有配置请编辑并保留原设置。
( set -C; printf '%s\n' FERTILE_MARKET_SOURCE=replay > "$FERTILE_ROOT/config/runtime.env" )
scripts/fertile ensure-up
scripts/fertile status --json
curl -fsS http://127.0.0.1:8090/api/v1/system/status
~~~

在部署机打开 `http://127.0.0.1:8090/dashboard` 查看服务和行情状态。只读访问无需管理令牌，回测提交需要 `WEB_ADMIN_TOKEN`。本地压测的部署复验使用 `tests/e2e-smoke.sh local --paper-only`。公网冒烟和巡检由普通服务器运维明确配置自己的 HTTPS origin 后启用。数据导入、提交和压测盒子运维见 [Ruler 手册](ops/ruler-runbook.md)。

## 运维和开发

| 工作 | 入口 |
| --- | --- |
| 普通服务器部署、systemd 与 HTTPS 反代 | [普通服务器部署指南](deploy/linux-server.md) |
| 构建、部署、回滚、重启与应急 | [部署清单](deploy/service.yaml) · [Ruler 手册](ops/ruler-runbook.md) |
| 暂存制品、升级与迁移回退 | [单进程热升级](deploy/hot-upgrade.md) · [受监管启停与手动排空](ops/ruler-runbook.md#受监管启停与手动排空) · [迁移前快照的隔离恢复](ops/ruler-runbook.md#从升级前快照回滚并切回新版) |
| 本机与公网健康、备份、恢复和告警 | [运维索引](ops/README.md) |
| 私人 Google Drive 加密备份、恢复点和离线演练 | [异地备份](ops/backup.md#私人-google-drive-加密备份) |
| OpenAPI 与生成客户端 | [契约快照与验证](contracts/README.md) |
| v1 T3a（#53）首批集成与隔离验收 | [集成说明](docs/v1-integration.md) |
| 工具链、配置与密钥 | [开发环境](docs/dev-environment.md) · [配置](docs/configuration.md) |
| 合成 fixture、私有数据与 Git/PR 检查 | [四仓数据发布政策](docs/data-policy.md) |
| 版本、子模块指针与脚本 | [发版规则](docs/release.md) · [脚本](scripts/README.md) |
| 架构、部署边界与协作 | [架构](docs/architecture.md) · [ADR-001](docs/adr/ADR-001-cloud-box-network-constraints.md) · [AGENTS.md](AGENTS.md) |

三个服务仓库的精确版本由总仓库 gitlink 锁定。agent 独立分发知识；服务部署定义统一维护在 deploy/。本次联调使用 0.0.0，正式发版另行决定。
