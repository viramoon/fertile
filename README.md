# fertile

fertile 将 finance 的行情与回测、web 的 HTTP 网关和前端看板部署在同一云盒子上。浏览器访问 web；web 经 HTTP 调用 finance。finance 将行情写入 Nautilus Parquet catalog，worker 从持久队列执行回测。PostgreSQL 保存 web 的 app 和 blog 数据。当前接入范围为 IBKR paper 只读行情。

## 如何使用

使用可读取三个私有子仓库的身份，在 Ubuntu 24.04 x86_64 盒子上以 box 用户运行。检出待部署的总仓库提交后，gitlink 会确定各组件版本。开发联调从 ha-runthrough 分支取得提交。

~~~sh
git clone --branch ha-runthrough https://github.com/viramoon/fertile.git /home/box/fertile-source
cd /home/box/fertile-source
git submodule update --init --recursive
scripts/bootstrap-box.sh
source /home/box/fertile/env.sh
scripts/fertile build
~~~

选择行情源后启动。paper 部署先按 [Gateway 操作说明](docs/ibkr-gateway.md) 安装和配置只读登录。离线体验可在全新测试盒子的持久配置中选择 replay，行情与 paper 使用独立数据目录。

~~~sh
mkdir -p "$FERTILE_ROOT/config"
# 仅在新测试实例上创建；已有配置请编辑并保留原设置。
( set -C; printf '%s\n' FERTILE_MARKET_SOURCE=replay > "$FERTILE_ROOT/config/runtime.env" )
scripts/fertile up
scripts/fertile status --json
curl -fsS http://127.0.0.1:8080/api/v1/system/status
~~~

打开本机 8080 端口即可访问看板。首页、仪表盘、博客和设置由同一个 web 进程托管。仪表盘展示系统状态、行情新鲜度和最近示例回测；准备样例及提交实际回测的命令见 [Ruler 手册](ops/ruler-runbook.md)。公网入口待域名确认后通过既有 Cloudflare Tunnel 启用。

## 运维和开发

| 工作 | 入口 |
| --- | --- |
| 构建、部署、回滚、重启与应急 | [部署清单](deploy/service.yaml) · [Ruler 手册](ops/ruler-runbook.md) |
| 本机与公网健康、备份、恢复和告警 | [运维索引](ops/README.md) |
| OpenAPI 与生成客户端 | [契约快照与验证](contracts/README.md) |
| 工具链、配置与密钥 | [开发环境](docs/dev-environment.md) · [配置](docs/configuration.md) |
| 版本、子模块指针与脚本 | [发版规则](docs/release.md) · [脚本](scripts/README.md) |
| 架构、部署边界与协作 | [架构](docs/architecture.md) · [ADR-001](docs/adr/ADR-001-cloud-box-network-constraints.md) · [AGENTS.md](AGENTS.md) |

三个服务仓库的精确版本由总仓库 gitlink 锁定。agent 独立分发知识；服务部署定义统一维护在 deploy/。本次联调使用 0.0.0，正式发版另行决定。
