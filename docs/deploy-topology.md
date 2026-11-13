# 部署拓扑与迁移边界

服务部署唯一来源是本仓库 [deploy/service.yaml](../deploy/service.yaml)，不引用 agent deploy-map 作为服务来源。agent 只同步 skill / prompt / 工作流，记录 agent commit，不属于业务服务。

| 模块 | 当前云电脑 + paper | 迁移后的边界 |
| --- | --- | --- |
| finance trader、collector、api 与 IB Gateway | 分进程，数据与状态位于配置化 /home/box 根目录 | 交易、实时数据和状态可独立迁往稳定服务器；api 边界与访问控制需验证 |
| web + frontend 静态文件 | axum 托管 SPA，经 Tunnel 入口 | 可留云电脑或随服务器迁移，不影响交易核心 |
| 研究、分析、回测 | 云电脑读取历史 Parquet catalog | 继续留云电脑，不进交易关键路径 |
| PostgreSQL app / blog | 归 web，持久化并备份 | 随 web 与业务数据需求迁移；不让交易依赖展示数据库 |
| catalog / Nautilus 节点恢复状态 | 归 finance，持久化方案与备份验证 | 随交易 / 数据边界迁移，状态后端 TBD |

仅 /home/box 按持久处理，系统改动需可重建；所有路径从配置读取，不硬编码 /srv 或 /etc。部署环境 config/base.yaml + cloudbox.yaml / stable.yaml 与 Backtest / Sandbox / Live、paper / 实盘是独立维度。

保留 [ADR-001 第 5 节](adr/ADR-001-cloud-box-network-constraints.md) 的可拆分原则；v2 将行情细化为 Parquet、交易基础交给 Nautilus、frontend 合并进 web 托管。TBD：实际目标、连接协议验证、域名、端口与迁移顺序，按后续迁移 issue 演练，不将固定出口代理当作长期稳定性方案。

## 已实现的环境引导

运行 scripts/bootstrap-box.sh 后 source /home/box/fertile/env.sh；Postgres init/start/status/stop 使用 scripts/postgres.sh，默认 data/postgres 与 run/postgres 均在持久根。工具版本以 [toolchain.env](../deploy/toolchain.env) 为准。重启后系统依赖由初始化器检查补装，工具 / 缓存 / 数据仍从 /home/box 读取，不依赖 systemd。所有 CI 使用 GitHub 托管 runner，不在盒子自建 runner。

密钥与覆盖顺序见 [配置规范](configuration.md)，Gateway 版本、IBC 对比和 paper 4002 恢复步骤见 [Gateway 方案](ibkr-gateway.md)。本次只准备工具和数据库，不部署或登录 Gateway，不开放真实交易。finance 的依赖获取已更新为源码 submodule，具体维护规则由 finance ADR-0003 取代旧 ADR-0002；系统边界与 HTTP / 静态托管关系不变。
