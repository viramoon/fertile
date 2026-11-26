# fertile 系统架构

fertile 是金融看板 + 自动交易系统的总仓库，负责初始化、集成、监控、运维与发布。finance、web、frontend 通过 submodule 绑定精确 commit；agent 是独立流程知识库，不是 submodule。

```text
本机浏览器 → web（axum，API + frontend 静态文件）→ HTTP → finance api
              ↓                                           ↕（持久化状态）
        PostgreSQL app / blog                   worker / collector → 回放或 paper Gateway
                                                     ↕
                                              Nautilus Parquet catalog
研究 / 回测（Rust + Python / PyO3）← 历史 catalog

agent → 通用 skill / prompt / 工作流分发（不是业务服务部署）
fertile deploy/ → 服务拓扑与环境配置；fertile tag → 部署与回滚单位
```

finance 以 NautilusTrader 为依赖、不 fork；Rust 内核和 Python 接口承担交易 / 回测基础，我们写策略、做市商 adapter、配置、进程入口、运维和研究。行情存 Parquet，不额外起时序数据库。collector 与 trader 分进程，api 独立，web 故障不影响交易，finance 不依赖 Tunnel。

web 使用 axum + sqlx + PostgreSQL，业务与博客 schema 归 web；调用 finance 的 HTTP API。frontend 为 React + TypeScript + Vite SPA，按功能组织，只调用 web，静态产物由 web 托管。finance 的 API 和 collector 独立运行，worker 从队列执行回测；trader 留给后续交易阶段。

OpenAPI 从 finance / web 代码导出（utoipa），快照在 [contracts](../contracts/README.md)，生成客户端禁止手改；实时消息另外定义。所有交易数值使用精确类型，不能用浮点传输 / 计算造成精度损失。

当前云电脑用于本地压力测试，使用 paper，部署复验与巡检均走本地。8090 为 web、8081 为 finance API、8082 为 collector、4002 为本机只读 paper Gateway。环境 profile 与运行模式独立，路径配置化，工具版本固定在 `deploy/toolchain.env`。未来生产服务器的公网入口、冒烟和巡检由运维按该服务器的 HTTPS origin 显式启用。迁移边界与网络证据见 [ADR-001](adr/ADR-001-cloud-box-network-constraints.md)。
