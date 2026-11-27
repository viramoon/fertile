# 文档与知识放置规则

| 载体 | 保存什么 | 更新方式 |
| --- | --- | --- |
| issue | 需求原话、实现方案和验收标准，作为历史记录 | 方案改版不覆盖原文，以评论追加版本、变化和理由，例如 v2 |
| PR | 怎么实现、测试结果、开发中问题及解决过程 | 关联 `Closes #<号>`，按验收标准自检 |
| 仓库文档 | 当前事实（current truth）与明确标注的设计 | 行为发生变化时，在同一个 PR 更新对应文档 |
| ADR | 重大决策、背景、备选和后果 | `docs/adr/ADR-<编号>-<标题>.md`，使用 [模板](adr/TEMPLATE.md)，后续变更另记 ADR |

原始需求与历史讨论留在 issue，PR 说明实现过程，文档只保留当前结论；仓库之外不维护第二份文档。文档统一用中文。目录中的 README 说明职责与边界，不代表该模块已经实现。

`TBD` 必须紧跟待确认事项、确认依据或负责阶段，不能用空文件代替说明。deploy/service.yaml 和 deploy/services.yaml 给出运行清单。普通服务器按部署指南操作，云盒子按 Ruler 手册和各服务 RUNBOOK 部署。

## 系统共同约定

| 仓库 | 在 fertile 系统中的职责 |
| --- | --- |
| [fertile](https://github.com/viramoon/fertile) | 金融看板 + 自动交易系统的总仓库；初始化、集成、监控、运维和发布 |
| [finance](https://github.com/viramoon/finance) | 主项目；基于 NautilusTrader 的策略、数据采集、交易、回测和 HTTP API |
| [web](https://github.com/viramoon/web) | axum 后端网关；调用 finance HTTP API，管理 PostgreSQL 业务与博客数据，托管前端静态文件 |
| [frontend](https://github.com/viramoon/frontend) | React + TypeScript + Vite 单页应用；只调用 web |
| [agent](https://github.com/viramoon/agent) | 独立的开发流程知识库；通用 skill、prompt、工作流、评估和复盘 |

fertile 以 git submodule 引入 finance、web、frontend；agent 是独立仓库，不是 submodule。常驻进程包括 finance-api、collector、回测 worker 和 web；frontend 构建产物由 web 托管，不单独起服务。

- **发布**：只有 fertile 对外带版本号。发布时更新各子模块指针 → 集成检查 → bump `VERSION` → 打 `vX.Y.Z` → 由发版脚本自动生成 `RELEASES.md` 条目（包含子模块 PR / issue），并自动给子模块打内部 tag `fertile-vX.Y.Z`。子模块不对外发版。Maas · Ruler 只按 fertile tag 部署和回滚。规则唯一入口是 [fertile docs/release.md](https://github.com/viramoon/fertile/blob/main/docs/release.md)，本次可运行脚本预览。
- **部署**：服务部署以 [fertile deploy/](https://github.com/viramoon/fertile/tree/main/deploy) 为唯一准绳。agent 的 deploy-map.yaml 只管 skill / prompt / 工作流分发，涉及服务只引用 fertile。
- **契约**：finance、web 的 OpenAPI 从代码导出（Rust 侧用 utoipa），快照存放于 [fertile contracts/](https://github.com/viramoon/fertile/tree/main/contracts)，CI 比对差异；不手写契约，不手改生成客户端。web ↔ finance 走 HTTP，frontend 只调用 web。实际导出和再生检查使用 scripts/contract-diff.sh。
- **配置**：部署环境使用 `config/base.yaml` + `config/cloudbox.yaml` 或 `config/stable.yaml`，profile 与运行模式是两个独立维度。finance 的运行模式为 Backtest / Sandbox / Live；paper 与实盘是 Live 下不同账户配置。本阶段云电脑只用 IBKR paper；未来迁移专用机器的实盘需另行验证。
- **路径与恢复**：云盒子只有 `/home/box` 按持久处理。普通服务器 `stable` 默认使用 `/var/lib/fertile`。两种模式均可覆盖持久根，工具和服务状态从该根解析。云盒子环境约束见 [ADR-001](adr/ADR-001-cloud-box-network-constraints.md)。
- **健康**：finance、web 提供 `/healthz`，区分 `live`（进程存活）与 `ready`（关键依赖可用且数据新鲜）。web/frontend 故障不影响 finance 交易核心，finance 不依赖 Cloudflare Tunnel。

## 本仓库文档索引

- [系统架构](architecture.md)
- [部署与迁移边界](deploy-topology.md)
- [唯一发版规则](release.md)
- [环境与网络约束](adr/ADR-001-cloud-box-network-constraints.md)
- [唯一服务部署来源](../deploy/README.md)
- [聚合服务清单](../deploy/service.yaml)
- [普通服务器部署](../deploy/linux-server.md)
- [热升级与本机接口](../deploy/hot-upgrade.md)
- [重启重建](../deploy/bootstrap.md)
- [运维索引](../ops/README.md)
- [代码导出快照](../contracts/README.md)
- [v1 T3a（#53）首批集成与隔离验收](v1-integration.md)
- [部署与校验脚本](../scripts/README.md)
- [自动发布记录说明](../RELEASES.md)
- [初始版本](../VERSION)

- [开发环境与 CI 策略](dev-environment.md)
- [配置与密钥](configuration.md)
- [四仓数据发布政策与接入](data-policy.md)
- [Gateway 方案](ibkr-gateway.md)
