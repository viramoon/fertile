# v1 T3a 首批集成与隔离验收

T3a 是 T3 的首批集成，PR #52 使用 `Closes #53` 与 `Refs #44`。#44 在全部批次验收后关闭。当前候选保留三个 gitlink，待 W6 与 U5 合并后统一前移。运行时核对见 [Ruler 手册](../ops/ruler-runbook.md#v1-t3a-首批运行时核对)。[T3a 验收合同](https://github.com/viramoon/fertile/issues/53) · [T3 父任务](https://github.com/viramoon/fertile/issues/44)

## 版本与契约

| 子仓 | 当前 gitlink | 消费方记录 |
| --- | --- | --- |
| finance | `19291f8a969266a4cc67e7d7a7f676754a87b839` | web 记录 `617ed83`。这两个版本导出相同的 OpenAPI，版本化协议及读流导出一致。 |
| web | `9132c5dc2e40f94d96151fbcc2989abbbb53d778` | frontend 仍用 `d9e5c79` 对应的 W4 契约。当前 web 已含 W5 与 W2a，两个阶段的 OpenAPI 分别验证。 |
| frontend | `3788ae986ea61762b4219fd17547b5e2840c93cb` | U2a、U2b、U3、U4 已合并。当前浏览器行情终端和期权页使用本地 fixture。 |

finance 在 `19291f8` 之后由 F6 扩展 bars 与指标查询，目标提交 `fc5e153` 又含后续协议变动。当前候选的 finance 导出与 web 输入一致。现有检查分别验证 W2a 的部署导出及前端实际使用的 W4 输入。T3a 的目标是 finance 至 F13、web 至 W2a+W6、frontend 至 U5 的同一条契约链。推进时先更新 web 使用的 finance 快照与客户端，再更新 frontend 使用的 web 快照与客户端，最后同时更新总仓 gitlink。[契约校验和导出流程](../contracts/README.md)

finance 读接口增加 WAL 产品、水位、事件、质量、最新投影及因子研究查询。价格、数量、序号与纳秒时钟沿用契约定义的精确十进制字符串。web 新增行情、自选、推送、集成测试和通知接口。`/api/v1/stream/market` 与 `/api/v1/events` 复用 web 的现有 HTTP 入口，分别使用 WebSocket 与 SSE。有效的期权 flow 和 exposure 查询仍返回 HTTP 503 与 `finance_contract_pending`。生产接口细节见 [finance API](https://github.com/viramoon/finance/blob/19291f8a969266a4cc67e7d7a7f676754a87b839/docs/api.md) 与 [web API](https://github.com/viramoon/web/blob/9132c5dc2e40f94d96151fbcc2989abbbb53d778/docs/api.md)。

## 当前候选的合成验收

CI 的 Integration 作业从本批 gitlink 构建制品。九只股票的回放 CSV 由 `tests/replay_fixture.py` 在临时目录生成，标的及价量使用固定向量，价量由整数计算，运行时钟由 ReplaySource 处理。测试全程使用合成数据、回放或本地替身。测试目录、队列和数据库均从空隔离实例创建，IBKR 仅以合成回放或内存 paper 只读替身验证。

| 验收项目 | 路径与判定 |
| --- | --- |
| 九标的后端回放 | `tests/replay_fixture.py` 生成固定的精确价量，`--verify-catalog` 从隔离 Parquet catalog 回读每个标的的首 3 根 bar，以十进制逐项核对 OHLC 和成交量。`tests/e2e-smoke.sh local --replay-nine` 经 web 的两个状态接口检查 QQQ、SPY、AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA 及各标的新鲜已落盘 bar。浏览器切换由前端自身的合成用例覆盖，实接待 U5 gitlink 前移。 |
| 期权敞口组件 | finance `option_exposure` 使用固定合约向量复算 GEX、wall、0DTE。frontend `options-u2b.test.tsx` 用本地合成数据验证敞口、观察和核验视图。生产期权上下游接线待后续批次。 |
| F12 独立复验 | 在 finance `f4786109e3812e4946adb92e97e513f1fbae5d79` 的独立归档和 Cargo target 中执行 `option_history` 及 `verified_paper_option_history_requests_and_decodes_synthetic_bars`。覆盖未到期合约的只读窗口、权限、精确价量、到期零请求及 OPRA 离线估算。F12 按总仓锁定 SHA 的验收待 finance gitlink 前移。 |
| W3 邮件 worker 组件链 | web 的 `notifications` 库及 API 测试覆盖合成事件写入、去重、outbox、超时重试、静默、限流和 SMTP 恢复。`postgres_rule_silence_records_events_and_summarizes_after_release` 调用 `record_signal`，经临时 PostgreSQL 的 outbox 验证 worker 及 SMTP 替身。另有预置 outbox 的重试用例。生产业务事件接入 outbox 待后续批次。 |
| F3 WAL 缺口与重置 | `market_ingress` 检查缺口报告、恢复分类及重启，`market_wal_projector` 检查持久水位、封段与投影。隔离恢复核对保留的合成 gap、reset 与新 epoch。断源补数后在 web 和浏览器展示质量待后续接线。 |
| T2b 全量与增量恢复 | `test_encrypted_wal_full_and_hourly_restore_gap_and_new_epoch` 从合成 WAL 发布 `rclone crypt` 本地全量和小时增量，在空目标核对父链、增量封段、恢复水位及恢复事件。 |
| 旧回测与迁移 | 现有冒烟继续经 web 提交旧示例与研究 fixture，确认 worker 结果，并检查旧深链返回的 SPA 入口。`published_queue_readback.py` 用两个制品读取扩展后的合成队列。`web-database.py` 检查旧库升级、完整迁移台账、数据库权限和恢复。 |
| PR 门禁与质量响应 | `tests/t3a_contract_test.py` 执行文档门禁，确认只写 `Refs #44` 会失败，并检查当前 W2a 的质量 schema 冲突与后续 web pin 的响应结构。web 的 `quality_publish` 测试使用合成输入验证真实发布命令和 HTTP router。 |

独立复验先按 [契约验证](../contracts/README.md#验证)从干净克隆检查 gitlinks 与生成物，再执行 [Integration](../.github/workflows/integration.yml) 和 [Recovery](../.github/workflows/recovery.yml) 的隔离用例。保留可公开的测试状态和计数。数据正文、邮件正文、数据库、日志和截图留在本地临时目录，处理边界见[四仓数据政策](data-policy.md)。

## 待统一前移与后续批次

- **T3a 统一前移。** W6 修复 web 导出 schema 冲突并对齐新版 finance，U5 使用新 web 契约接入终端实时数据。待协调者统一前移三个 gitlink 后，复验完整契约链、锁定 finance SHA 的 F12 及浏览器九标的实接。F9b 调度和 F13 进程拆分随 finance 前移。
- **质量接口。** W2a 覆盖了 W4 的 `MarketWatermarks` 和 `SourceObservation`。当前 `/api/v1/quality/gaps` 的导出指向 finance 的 `access`、`as_of`、`watermarks` 结构，运行时水位使用 `local_durable`、`projected`、`remote_verified`、`source_latest`。W6 通过 web 的导出流程修复。断源、补数与浏览器质量报告贯通验收随上游接线推进。
- **期权成功输出。** F9e 的观察与核验证据、F10 后续批次和 W2b 的期权读取成功响应待各子仓交付，再复验 HTTP 与浏览器路径。
- **业务邮件与私有部署。** 生产业务事件入 outbox 的接线待后续批次。SMTP/Drive 账户、容量、收件人、离线密钥和行情许可由 vira 确认。外部备份、Binance 与 notifier 保持默认关闭。
