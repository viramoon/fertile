# 代码导出的接口快照

| 快照 | 生产者 | 直接消费者 |
| --- | --- | --- |
| [finance.openapi.json](finance.openapi.json) | finance-api 的 utoipa 导出命令 | web 的 OpenAPI Generator Rust 客户端，位于 `web/crates/finance-client` |
| [web.openapi.json](web.openapi.json) | web 的 utoipa 导出命令 | frontend 的 Hey API 客户端，位于 `frontend/src/api/generated` |

快照保留 gitlink 对应代码导出的原始字节。消费者分别保存输入快照和生产者的完整提交号，见 `web/openapi/finance.commit` 与 `frontend/openapi/web.commit`。Finance 的版本化市场协议位于子仓 `protocol/v1/` 和 `protocol/v2/`，其中 v1 读流包含 WebSocket、SSE 和合成消息样例。W2a 浏览器接口的 WebSocket 指令及 SSE 负载由 web 的 OpenAPI 导出。

部署控制器的热升级协议通过本机进程调用，定义了候选就绪、旧版回读、采集水位交接与回退证据。[热升级与本机接口](../deploy/hot-upgrade.md)

接口示例、生成快照及客户端测试输入适用 [四仓数据发布政策](../docs/data-policy.md)。行情 fixture 可复现并登记合成来源，CI 报告只发布获准公开的摘要。

## 验证

检出 fertile 对应的子模块，使用锁定的 Rust（含 rustfmt）、Node 和 pnpm，准备 JDK 17 或更高版本（含 `javac`）、`unzip`、`patch` 和 `flock`。T3a 回归使用 Python 3 和 `PyYAML==6.0.3`。Integration 通过 `scripts/setup-supervisor.sh` 安装锁定依赖，文档门禁的子进程从同一虚拟环境的解释器路径启动。在仓库根目录运行下列命令。

```sh
git submodule update --init --recursive
pnpm --dir frontend install --frozen-lockfile
bash scripts/contract-diff.sh
bash tests/contract-drift.sh
python3 -B tests/t3a_contract_test.py
```

检查将三个子模块的 HEAD 与 fertile 的 Git 索引逐一核对，从锁定提交的临时 Git 归档重新导出两份 OpenAPI，再与快照逐字节比对。消费方记载的提交允许是同树 squash 前提交。若记载的是生产者 gitlink 的祖先，两份独立导出的 HTTP OpenAPI 必须逐字节一致。Finance 祖先还必须有相同的版本化协议文件，两边都重新导出并比对 v1 读流的三份产物。两个消费方继续用原有生成器按各自输入验证客户端。导出使用 `CARGO_TARGET_DIR` 下的独立 `contract-exports` 缓存，持锁按顺序解包，解包时刷新源码时间戳，确保 Cargo 能识别不同提交的源码。依赖已经缓存时可通过 `CARGO_NET_OFFLINE=true` 离线运行。已有固定版本的 OpenAPI Generator JAR 时，可设置 `OPENAPI_GENERATOR_JAR`，web 仍会校验 SHA256。

负向演练在一次性克隆中分别改动快照格式、生产者提交号、Rust 和 TypeScript 生成代码及版本化读流文件。生产者提交号用例覆盖缺失提交和非祖先的不同 Git 树。W4 过渡条件只接受此前核对的固定提交组合。锁定 W6 时，演练还会切换至仍使用 W2a 快照的 U5 gitlink，核对契约检查拒绝该组合。

### T3a（#53）当前候选

W6 `6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06` 的 `openapi/finance.commit` 精确匹配 finance `fc5e153e9727181f5748659c0610b2fbc2b49769`。finance 新增 F6 bars 与 indicators 路径，F4 在 v2 事件与能力 schema 中加入 `funding_rate`、`open_interest` 和相关控制帧。W6 保存同一 finance 提交导出的三份 1.0 读流产物。F12 独立测试目录由总仓锁定的 finance gitlink 归档创建。

前端 U6 `dda817d41cd9b66d75a49b70946ca38790e2fe7b` 的 `openapi/web.commit` 精确匹配 W6 `6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06`。`frontend/openapi/web.openapi.json` 与 W6 生产者快照及总仓快照逐字节一致。U6 重新生成客户端，恢复适配器直接使用 `QualityGapsPage`，系统页从覆盖项读取 `QualityWatermarks`。与 W2a 相比，W6 导出的差异涉及 `Event`、`Message`、`FundingRate`、`OpenInterest` 与质量水位组件。

W6 将 `/api/v1/quality/gaps` 使用的水位和源序号组件分别导出为 `QualityWatermarks` 和 `QualitySourceObservation`。质量接口水位字段为 `local_durable`、`projected`、`remote_verified`、`source_latest`。`/api/v1/market/watermarks` 沿用 finance 的 `MarketWatermarks` 和 `SourceObservation`。总仓测试检查质量响应结构，子仓 `quality_publish` 使用合成输入验证发布及读取路径。[T3a 验收范围](../docs/v1-integration.md)

### 集成 CI 的生成器下载

[跨仓集成工作流](../.github/workflows/integration.yml)从锁定的 web 生成脚本读取 Maven Central 下载地址、版本和 SHA-256，以 `fertile-openapi-generator-<版本>-<SHA-256>` 精确匹配 Actions 缓存。缓存路径只包含 runner 临时目录里的单个公开 JAR。命中时在本地校验 JAR，无需访问 Maven Central。校验失败会停止作业，遇到损坏的缓存可删除对应键后重跑。

未命中时以 1、2、4 秒退避重试 HTTP 429、408、5xx 和常见网络故障，共最多四次尝试。每次连接最多等待 10 秒，单次下载最多 60 秒。下载先写入临时文件，通过 SHA-256 校验后才移入缓存路径并保存。后续契约检查和漂移演练共享 `OPENAPI_GENERATOR_JAR`，web 生成脚本仍执行自己的 SHA-256 校验。

## 更新

1. 生产者完成接口变更并提交。通过 web 的 `scripts/gen-finance-client.sh from-finance` 或 frontend 的 `scripts/gen-web-client.sh from-web` 引入对应生产者的提交，审查生成差异并测试消费者。直接运行这两个子仓归档生成命令时，为不同生产者提交指定独立的 `CARGO_TARGET_DIR`，避免归档的原始时间戳导致 Cargo 复用另一提交的制品。
2. 将新提交推送各自仓库，在 fertile 检出并暂存对应 gitlink。以 `bash scripts/contract-diff.sh --write` 从锁定的代码重新生成本目录两份快照。脚本会先验证消费方输入与生成代码，再写入两份输出。
3. 重跑契约检查，将两个子模块之间的接口变更、对应测试结果及升级影响写入集成 PR。快照与客户端均通过各自生成流程更新。

跨 HTTP 的价格、数量、金额使用精确类型与明确的序列化格式。评审应区分新增、兼容变更和破坏性变更。删除字段或改变字段类型、必填性时，需要关联 issue、生产者与消费者的发布顺序、兼容窗口、回滚方案及验收证据。跨仓依赖需要协调时由 CTO bot 拆为关联 issue。快照随 fertile 版本一起发布。
