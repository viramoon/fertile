# 代码导出的接口快照

| 快照 | 生产者与消费者 |
| --- | --- |
| finance.openapi.json | finance 的 Rust API 用 utoipa / utoipa-axum 导出，web 用它生成 finance_client（工具 TBD，候选 progenitor） |
| web.openapi.json | web 的 handler 用 utoipa / utoipa-axum 导出，frontend 用 Hey API + TanStack Query 插件生成客户端 |

快照是代码导出的结果，不是手写契约；禁止手改快照与生成客户端。当前无实现，故此目录只有 README，不创建空 JSON 假快照。WebSocket / SSE 不在 OpenAPI 范围内，实时消息需单独协议。

## 更新流程

先修改生产者代码并完成验证 → 从同一已推送 commit 导出 → 在 fertile PR 更新对应子模块指针与快照 → 差异检查和评审 → 生成消费者客户端并验证 → 按集成 PR 更新消费者指针。需协调依赖时由 CTO bot 拆为关联 issue，不能把不兼容组合发布。

CI 比对设计：从锁定指针重新导出并规范化，与提交快照比较；分类新增、兼容变更和破坏性变更；出现删除、字段类型 / 必填变化等破坏项时要求迁移与兼容计划。TBD：工具、生成命令、规范化规则和门禁实现，见 [scripts](../scripts/README.md)，本次不实现比对。

跨 HTTP 的价格、数量、金额使用精确类型与明确序列化，不能用浮点消除类型差异。破坏性变更必须有关联 issue、生产者 / 消费者发布顺序、兼容窗口、回滚与验收证据；快照本身不独立对外发版。
