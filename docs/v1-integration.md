# v1 T3a 首批集成与隔离验收

T3a 是 T3 的首批集成，PR #52 使用 `Closes #53` 与 `Refs #44`。#44 在全部批次验收后关闭。本轮使用协调者指定的 F13、W6 和 U5 提交。U5 记录的上游仍为 W2a，契约比对要求 frontend 先对 W6 重新生成。运行时核对见 [Ruler 手册](../ops/ruler-runbook.md#v1-t3a-首批运行时核对)。[T3a 验收合同](https://github.com/viramoon/fertile/issues/53) · [T3 父任务](https://github.com/viramoon/fertile/issues/44)

## 版本与契约

| 子仓 | 当前 gitlink | 消费方记录 |
| --- | --- | --- |
| finance | `fc5e153e9727181f5748659c0610b2fbc2b49769` | W6 的 `openapi/finance.commit` 精确匹配。包括 F6、F9b、F4、F12、F13。 |
| web | `6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06` | U5 的 `openapi/web.commit` 记录 `9132c5dc2e40f94d96151fbcc2989abbbb53d778`。W6 的导出已修复质量水位 schema，并扩展行情事件类型，两版 OpenAPI 存在字节差异。 |
| frontend | `adce47d05acbf58aeb95c4bd8562ef7ebdf36f16` | U5 默认接入同源 W2a，并保留 `?market=replay` 显式合成功能。仍需与 W6 对齐生成客户端。 |

两份快照由本候选锁定的生产者直接导出。当前 W6 与 U5 的差异会阻断统一契约检查。完成 frontend 的 W6 再生成、应用层类型适配并合入 main 后，更新总仓 frontend gitlink 并重跑完整检查。[契约校验和导出流程](../contracts/README.md)

finance 新增 `/api/v1/market/bars`、`/api/v1/indicators` 与 `/api/v1/indicators/values`。价格、数量、指标值、序号及纳秒时钟均按各自契约传输精确十进制文本。W6 的现有 bars 与指标端点仍使用 WAL 视图。市场 WebSocket `/api/v1/stream/market` 继续发送 1.0 帧，对 2.0 事件发出 `unsupported_version` 重置。运维 SSE `/api/v1/events` 复用现有 web 入口。有效的期权 flow 和 exposure 查询仍返回 HTTP 503 与 `finance_contract_pending`，U5 默认页面显示相应待接入状态。[finance API](https://github.com/viramoon/finance/blob/fc5e153e9727181f5748659c0610b2fbc2b49769/docs/api.md) · [web API](https://github.com/viramoon/web/blob/6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06/docs/api.md)

## 当前候选的合成验收

CI 的 Integration 作业从本批 gitlink 构建制品。九只股票的回放 CSV 由 `tests/replay_fixture.py` 在临时目录生成，标的及价量使用固定向量，价量由整数计算，运行时钟由 ReplaySource 处理。测试全程使用合成数据、回放或本地替身。测试目录、队列和数据库均从空隔离实例创建，IBKR 仅以合成回放或内存 paper 只读替身验证。

| 验收项目 | 路径与判定 |
| --- | --- |
| 九标的回放 | `tests/replay_fixture.py` 生成固定的精确价量，`--verify-catalog` 从隔离 Parquet catalog 回读每个标的的首 3 根 bar，以十进制逐项核对 OHLC 和成交量。`tests/e2e-smoke.sh local --replay-nine` 经 web 的两个状态接口检查 QQQ、SPY、AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA 及各标的新鲜已落盘 bar。U5 的 `market-web.test.tsx` 以合成的同源 W2a HTTP、WS、SSE 响应验证浏览器适配层。与 W6 一起实测须完成客户端对齐。 |
| 期权敞口组件 | finance `option_exposure` 使用固定合约向量复算 GEX、wall、0DTE。frontend `options-u2b.test.tsx` 用本地合成数据验证敞口、观察和核验视图。生产期权上下游接线待后续批次。 |
| F12 期权历史 | `tests/finance-f12.sh` 校验 finance checkout 与 `HEAD:finance` 一致，从该 gitlink 的独立归档执行 `option_history` 及 `verified_paper_option_history_requests_and_decodes_synthetic_bars`。回归检查确认暂存的 pin 和未提交文件均无法改变测试源码，并拒绝不匹配的 checkout。用例覆盖未到期合约的只读窗口、权限、精确价量、到期零请求及 OPRA 离线估算。 |
| W3 邮件 worker 组件链 | web 的 `notifications` 库及 API 测试覆盖合成事件写入、去重、outbox、超时重试、静默、限流和 SMTP 恢复。`postgres_rule_silence_records_events_and_summarizes_after_release` 调用 `record_signal`，经临时 PostgreSQL 的 outbox 验证 worker 及 SMTP 替身。另有预置 outbox 的重试用例。生产业务事件接入 outbox 待后续批次。 |
| F3 WAL 缺口与重置 | `market_ingress` 检查缺口报告、恢复分类及重启，`market_wal_projector` 检查持久水位、封段与投影。隔离恢复核对保留的合成 gap、reset 与新 epoch。断源补数后在 web 和浏览器展示质量待后续接线。 |
| T2b 全量与增量恢复 | `test_encrypted_wal_full_and_hourly_restore_gap_and_new_epoch` 从合成 WAL 发布 `rclone crypt` 本地全量和小时增量，在空目标核对父链、增量封段、恢复水位及恢复事件。 |
| 旧回测与迁移 | 现有冒烟继续经 web 提交旧示例与研究 fixture，确认 worker 结果，并检查旧深链返回的 SPA 入口。`published_queue_readback.py` 用两个制品读取扩展后的合成队列。`web-database.py` 检查旧库升级、完整迁移台账、数据库权限和恢复。 |
| PR 门禁与质量响应 | `tests/t3a_contract_test.py` 执行文档门禁，确认只写 `Refs #44` 会失败。W6 将质量水位与源序号导出为 `QualityWatermarks` 和 `QualitySourceObservation`，质量接口响应字段沿用 `local_durable`、`projected`、`remote_verified`、`source_latest`。web 的 `quality_publish` 用合成输入验证真实发布命令和 HTTP router。 |

独立复验先按 [契约验证](../contracts/README.md#验证)从干净克隆检查 gitlinks 与生成物，再执行 [Integration](../.github/workflows/integration.yml) 和 [Recovery](../.github/workflows/recovery.yml) 的隔离用例。保留可公开的测试状态和计数。数据正文、邮件正文、数据库、日志和截图留在本地临时目录，处理边界见[四仓数据政策](data-policy.md)。

## 契约接续与后续批次

- **T3a 契约接续。** frontend 对 W6 再生成时，`src/api/recoveryWeb.ts` 中兼容 W2a 包装结构的分支会产生 TypeScript `TS2322`。应随客户端更新适配该处的质量水位类型，合入 main 后再前移总仓 frontend gitlink，复验契约链和浏览器九标的实接。
- **质量接口。** 断源、补数与浏览器质量报告贯通验收随上游接线推进。
- **期权成功输出。** F9e 的观察与核验证据、F10 后续批次和 W2b 的期权读取成功响应待各子仓交付，再复验 HTTP 与浏览器路径。
- **业务邮件与私有部署。** 生产业务事件入 outbox 的接线待后续批次。SMTP/Drive 账户、容量、收件人、离线密钥和行情许可由 vira 确认。外部备份、Binance 与 notifier 保持默认关闭。
