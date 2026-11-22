# 代码导出的接口快照

| 快照 | 生产者 | 直接消费者 |
| --- | --- | --- |
| [finance.openapi.json](finance.openapi.json) | finance-api 的 utoipa 导出命令 | web 的 OpenAPI Generator Rust 客户端，位于 `web/crates/finance-client` |
| [web.openapi.json](web.openapi.json) | web 的 utoipa 导出命令 | frontend 的 Hey API 客户端，位于 `frontend/src/api/generated` |

快照保留导出命令产生的原始字节。消费者分别保存输入快照和生产者的完整提交号，见 `web/openapi/finance.commit` 与 `frontend/openapi/web.commit`。WebSocket 和 SSE 的消息协议需单独维护。

## 验证

检出 fertile 对应的子模块，使用锁定的 Rust、Node 和 pnpm，准备 JDK 17 或更高版本（含 `javac`）、`unzip` 与 `patch`。在仓库根目录运行下列命令。

```sh
git submodule update --init --recursive
pnpm --dir frontend install --frozen-lockfile
bash scripts/contract-diff.sh
bash tests/contract-drift.sh
```

检查将三个子模块的 HEAD 与 fertile 的 Git 索引逐一核对，并检查消费者记录的生产者提交号。两个原有生成器分别重新生成客户端，与已检出的代码逐字节比对。两份 OpenAPI 由生产者锁定提交的临时 Git 归档重新导出，再与快照比对。可指定 `CARGO_TARGET_DIR` 复用 Cargo 编译缓存；已有固定版本的 OpenAPI Generator JAR 时，可设置 `OPENAPI_GENERATOR_JAR`，web 仍会校验 SHA256。

负向演练在一次性克隆中分别改动快照格式、生产者提交号、Rust 和 TypeScript 生成代码，四次检查都须因对应差异失败。

## 更新

1. 生产者完成接口变更并提交。通过 web 的 `scripts/gen-finance-client.sh from-finance` 或 frontend 的 `scripts/gen-web-client.sh from-web` 引入对应生产者的提交，审查生成差异并测试消费者。
2. 将新提交推送各自仓库，在 fertile 检出并暂存对应 gitlink。以 `bash scripts/contract-diff.sh --write` 从锁定的代码重新生成本目录两份快照。脚本会先验证消费方输入与生成代码，再写入两份输出。
3. 重跑契约检查，将两个子模块之间的接口变更、对应测试结果及升级影响写入集成 PR。快照与客户端均通过各自生成流程更新。

跨 HTTP 的价格、数量、金额使用精确类型与明确的序列化格式。评审应区分新增、兼容变更和破坏性变更。删除字段或改变字段类型、必填性时，需要关联 issue、生产者与消费者的发布顺序、兼容窗口、回滚方案及验收证据。跨仓依赖需要协调时由 CTO bot 拆为关联 issue。快照随 fertile 版本一起发布。
