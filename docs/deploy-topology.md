# 部署拓扑与迁移边界

服务部署唯一来源是本仓库 [deploy/service.yaml](../deploy/service.yaml)，不引用 agent deploy-map 作为服务来源。agent 只同步 skill / prompt / 工作流，记录 agent commit，不属于业务服务。

| 模块 | 云盒子压测 + paper | 普通服务器部署边界 |
| --- | --- | --- |
| finance collector、API 与 IB Gateway | 分进程，数据与状态位于配置化 `/home/box` 根目录 | 核心服务使用 stable profile，API 默认走环回，paper Gateway 可由运维单独管理 |
| web + frontend 静态文件 | axum 托管 SPA，本机环回访问 | 同版本构建，默认通过 `127.0.0.1:8090` 访问，由运维按需配置 HTTPS 入口 |
| 研究、分析、回测 | 云电脑读取历史 Parquet catalog | 继续留云电脑，不进交易关键路径 |
| PostgreSQL app / blog | 归 web，持久化并备份 | 随 web 与业务数据需求迁移；不让交易依赖展示数据库 |
| catalog / Nautilus 节点恢复状态 | 归 finance，持久化方案与备份验证 | 随交易 / 数据边界迁移，状态后端 TBD |

云盒子仅 `/home/box` 按持久处理，`cloudbox` 默认持久根为 `/home/box/fertile`。普通服务器用 `stable`，默认 `/var/lib/fertile`。两个 profile 均可用 `FERTILE_ROOT` 或 `FERTILE_BOX_HOME` 覆盖。`config/base.yaml` 加对应 profile 文件用于部署环境选择，Backtest / Sandbox / Live 与 paper / 实盘由各自业务配置决定。

保留 [ADR-001 第 5 节](adr/ADR-001-cloud-box-network-constraints.md) 的可拆分原则。行情存入 Parquet，交易基础交给 Nautilus，frontend 由 web 托管。实际生产主机、HTTPS 域名和数据迁移窗口由运维确定。

## 已实现的环境引导

运行 `scripts/bootstrap-box.sh` 后执行 `source "$FERTILE_ROOT/env.sh"`。工具版本以 [toolchain.env](../deploy/toolchain.env) 为准。PostgreSQL 的 `data/postgres` 与 `run/postgres` 均相对于持久根。普通服务器可用 systemd 开机启动核心五项与已启用的扩展服务，容器由 tini 启动前台 supervisord，两者读取同一个进程清单。云盒子重启后由 Ruler 检查系统依赖，并按[重启检查](../ops/ruler-runbook.md#重启与外部入口)核对发布和数据目标后恢复服务。所有 CI 使用 GitHub 托管 runner。[普通服务器部署指南](../deploy/linux-server.md) [云盒子守护 ADR](adr/ADR-002-process-supervision.md)

默认各 HTTP 服务只在环回监听，外部浏览器通过可选的 HTTPS 反向代理连接 web。PostgreSQL 与 finance 的内部端口保留在本机。公网检查需要运维显式配置。密钥与覆盖顺序见 [配置规范](configuration.md)，Gateway 版本、IBC 对比和 paper 4002 恢复步骤见 [Gateway 方案](ibkr-gateway.md)。
