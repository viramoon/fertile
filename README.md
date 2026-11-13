# fertile

金融看板 + 自动交易系统的总仓库（super repo），负责初始化、集成、监控、运维和发布。以 git submodule 引入 finance、web、frontend；agent 是独立仓库，不是 submodule。

本仓库按 [issue #1 方案 v2](https://github.com/viramoon/fertile/issues/1#issuecomment-6834605011) 初始化。当前已有 [云盒子初始化脚本](scripts/bootstrap-box.sh) 与 Postgres 生命周期命令，业务服务仍处骨架阶段；TBD 项都需后续 issue 明确后再实施。开发从 [AGENTS.md](AGENTS.md) 开始。

## 系统中的位置

| 仓库 | 在 fertile 系统中的职责 |
| --- | --- |
| [fertile](https://github.com/viramoon/fertile) | 金融看板 + 自动交易系统的总仓库；初始化、集成、监控、运维和发布 |
| [finance](https://github.com/viramoon/finance) | 主项目；基于 NautilusTrader 的策略、数据采集、交易、回测和 HTTP API |
| [web](https://github.com/viramoon/web) | axum 后端网关；调用 finance HTTP API，管理 PostgreSQL 业务与博客数据，托管前端静态文件 |
| [frontend](https://github.com/viramoon/frontend) | React + TypeScript + Vite 单页应用；只调用 web |
| [agent](https://github.com/viramoon/agent) | 独立的开发流程知识库；通用 skill、prompt、工作流、评估和复盘 |


系统关系、版本、契约、配置、健康和环境规则见 [docs/README.md](docs/README.md)。服务部署唯一来源是 fertile deploy/，agent 只分发知识；只有 fertile 对外发版，Maas · Ruler 只按 fertile tag 部署 / 回滚。
## 获取与子模块

```sh
git clone --recurse-submodules https://github.com/viramoon/fertile.git
cd fertile
git submodule update --init --recursive
```

.gitmodules 的 branch=main 说明更新来源，部署与复现仍使用 gitlink 锁定的 commit。更新指针只能通过 PR；先推子模块、再推总仓库，目标必须已推送且属于各自 main 历史。

```text
fertile ── submodule ── finance（Nautilus / Parquet / HTTP API）
        ├─ submodule ── web（axum / PostgreSQL / 前端静态托管）
        └─ submodule ── frontend（Vite SPA，只调用 web）
agent（独立）── skill / prompt / 工作流分发
```

## 目录说明

```text
fertile/
├── README.md / AGENTS.md / VERSION / RELEASES.md
├── .gitmodules             finance / web / frontend，跟踪 main
├── finance/ web/ frontend/ gitlink 锁定精确 commit
├── .agents/skills/          本仓库专属 skill 约定
├── contracts/              代码导出快照规则，当前无假快照
├── deploy/                 唯一服务部署清单与 bootstrap 设计
├── ops/                    健康、监控与 Maas · Ruler 手册
├── scripts/                发版、子模块、契约脚本的设计，无实现
├── docs/                   架构、拓扑、唯一发版规则与 ADR-001
└── .github/                模板与文档检查
```

目录中的 README 说明职责和边界。VERSION 与 .gitmodules、gitlink 是 v2 明确要求的版本和组织元数据，其他新增内容为 Markdown / YAML 文档与文档 CI。

## 文档索引

- [AGENTS.md](AGENTS.md)：开发流程与硬规则
- [docs/README.md](docs/README.md)：知识放置、系统约定与文档索引
- [系统架构](docs/architecture.md)
- [部署与迁移边界](docs/deploy-topology.md)
- [唯一发版规则](docs/release.md)
- [环境与网络约束](docs/adr/ADR-001-cloud-box-network-constraints.md)
- [唯一服务部署来源](deploy/README.md)
- [聚合服务设计](deploy/service.yaml)
- [重启重建](deploy/bootstrap.md)
- [运维索引](ops/README.md)
- [代码导出快照](contracts/README.md)
- [未来脚本设计](scripts/README.md)
- [自动发布记录说明](RELEASES.md)
- [初始版本](VERSION)

## 协作与检查

一个任务对应一个 issue，使用 issue-<号>-<简述> 分支和 PR，CI 通过后合并，禁止直接推送 main。PR 需含 Closes #<号>、测试结果和逐条验收自检；开发问题在原分支解决，文档随行为在同一 PR 更新。文档 CI 检查必需文件、非空内容、YAML、相对链接及 PR 描述，不编译业务代码；这不代表已经完成运行或真实交易验证。

## 环境与重启上手

- [开发环境与工具链](docs/dev-environment.md)
- [密钥与配置分层](docs/configuration.md)
- [IBKR Gateway 部署方案（未安装、未登录）](docs/ibkr-gateway.md)
- [初始化集成测试](tests/bootstrap-box.sh)

当前工具链与 Postgres 引导已实现，系统发布 / 子模块检查 / 契约比对脚本仍由后续 issue 实现；VERSION 保持 0.0.0。
