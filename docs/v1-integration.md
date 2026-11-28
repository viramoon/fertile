# v1 跨仓集成与隔离验收

## T3b 第 2 批

本批集成 PR 使用 `Closes #54` 与 `Refs #44`。#44 在全部批次验收后关闭。联调版本为 `0.0.0`，部署和发版另行安排。[T3b 验收合同](https://github.com/viramoon/fertile/issues/54) · [T3 父任务](https://github.com/viramoon/fertile/issues/44)

| 子仓 | 当前 gitlink | 交付和锁定关系 |
| --- | --- | --- |
| finance | `86dcc8604b6881d648418a25e6cd13c24d4a9b01` | F9e、F10b、F10c、F10d、F3b、F14。与 web 的 `openapi/finance.commit` 精确匹配。 |
| web | `fcfda737d5152518d1d77cd55a5287c7185f3fb7` | W2b、W7、W8、W9，含 W10、W11、W13 与同源回环会话。由 frontend 的 `openapi/web.commit` 精确引用。 |
| frontend | `731d73dfaa10e726ee60e152c3b93f591c78728f` | U7 使用锁定的生成客户端，接入策略、回测、因子与期权页面。`?market=replay` 或 `?research=replay` 分别选择显式合成回放。 |

独立复验使用从干净克隆构建的锁定制品。先运行 `bash scripts/contract-diff.sh --check-pins` 和完整导出检查，再运行 `python3 -B tests/t3b_integration.py --release "$release"`，其中 `release` 指向本次构建目录。编译复用锁定的离线 Cargo 依赖。隔离验收仅使用合成回放、进程内合成行情、临时回环进程和 Unix socket PostgreSQL，退出时停止进程。IBKR 部署接入仅为 paper 只读。测试结果保留用例状态与计数，数据正文、数据库、日志和截图使用本地临时目录。[契约验证](../contracts/README.md#验证) · [跨仓集成工作流](../.github/workflows/integration.yml) · [四仓数据政策](data-policy.md)

| 验收项目 | 路径与判定 |
| --- | --- |
| 策略上传至结果读回 | `tests/t3b_strategy.py` 经 web 上传校验、登记和选版，对照计划与内容哈希，再用同一版提交配置作业，回读 finance worker 的终态和结果。 |
| 匿名公开作业续页 | `tests/t3b_strategy.py` 通过 `GET /api/v1/backtests` 获取并传回 `next_cursor`，核对续页跨过管理员可见的配置作业。公开作业类型为 `DemoStrategy` 和 `SmaCrossover`。续页以最后一个可见 ID 为界，终页游标为 `null`。 |
| 期权链、场所深度和敞口 | `tests/t3b_market.py` 以合成行情所有者装配 finance 读契约，经 web 请求期权链、Nasdaq 场所深度和敞口并核对响应。frontend 的 `options-web.test.tsx`、`market-web.test.tsx` 从合成的同源响应验证浏览器适配层。 |
| 期权流及策略页面 | `tests/t3b_market.py` 核对 W9 观察与核验的十进制证据、覆盖和续页。U7 通过生成 SDK 渲染。frontend `options-web.test.tsx` 检查来源与空白状态，`research-web.test.tsx` 检查上传、版本、作业和因子页面。 |

切换时核对 web 迁移终点 `20261127074531`，finance 的 SQLite `jobs_legacy_id` 首次打开时自动建立。W2b、W8、W9 沿用现有端口、进程及环境变量。独立 finance API 在期权和场所深度来源未装配时向 web 返回来源不可用，成功响应需由获授权的行情所有者接入。F3 路径、默认关闭开关、邮件快照与回退安排见 [第 2 批运行时核对](../ops/ruler-runbook.md#v1-t3b-第-2-批运行时核对)。

## T3a 首批验收

T3a 是 T3 的首批集成，PR #52 使用 `Closes #53` 与 `Refs #44`。以下记录 finance F13、web W6、frontend U6 的首批范围与回归。[T3a 运行时核对](../ops/ruler-runbook.md#v1-t3a-首批运行时核对) · [T3a 验收合同](https://github.com/viramoon/fertile/issues/53)

### 版本与契约

| 子仓 | T3a gitlink | 消费方记录 |
| --- | --- | --- |
| finance | `fc5e153e9727181f5748659c0610b2fbc2b49769` | W6 的 `openapi/finance.commit` 精确匹配。包括 F6、F9b、F4、F12、F13。 |
| web | `6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06` | U6 的 `openapi/web.commit` 精确匹配，frontend 输入快照与 W6 的导出逐字节一致。 |
| frontend | `dda817d41cd9b66d75a49b70946ca38790e2fe7b` | U6 使用 W6 生成客户端。浏览器默认使用同源 web，`?market=replay` 显式选择合成回放。 |

两份总仓快照由锁定的生产者导出。U6 的恢复适配器直接使用生成的 `QualityGapsPage`，系统页以十进制文本展示持久位置、投影位置、远端已验证位置和源观测。未获得的观测显示「未知」，有效的零位置显示 `0`。[契约校验和导出流程](../contracts/README.md)

finance 新增 `/api/v1/market/bars`、`/api/v1/indicators` 与 `/api/v1/indicators/values`。价格、数量、指标值、序号及纳秒时钟均按各自契约传输精确十进制文本。W6 的现有 bars 与指标端点仍使用 WAL 视图。市场 WebSocket `/api/v1/stream/market` 继续发送 1.0 帧，对 2.0 事件发出 `unsupported_version` 重置。运维 SSE `/api/v1/events` 复用现有 web 入口。有效的期权 flow 和 exposure 查询仍返回 HTTP 503 与 `finance_contract_pending`，frontend 默认页面显示相应待接入状态。[finance API](https://github.com/viramoon/finance/blob/fc5e153e9727181f5748659c0610b2fbc2b49769/docs/api.md) · [web API](https://github.com/viramoon/web/blob/6e5fd80a56c3ab2fd0634fd69ecec3b7315dcd06/docs/api.md)

### 首批合成验收

CI 的 Integration 作业从本批 gitlink 构建制品。九只股票的回放 CSV 由 `tests/replay_fixture.py` 在临时目录生成，标的及价量使用固定向量，价量由整数计算，运行时钟由 ReplaySource 处理。测试全程使用合成数据、回放或本地替身。测试目录、队列和数据库均从空隔离实例创建，IBKR 仅以合成回放或内存 paper 只读替身验证。

| 验收项目 | 路径与判定 |
| --- | --- |
| 九标的回放 | `tests/replay_fixture.py` 生成固定的精确价量，`--verify-catalog` 从隔离 Parquet catalog 回读每个标的的首 3 根 bar，以十进制逐项核对 OHLC 和成交量。`tests/e2e-smoke.sh local --replay-nine` 经 web 的两个状态接口检查 QQQ、SPY、AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA 及各标的新鲜已落盘 bar。frontend 的 `market-web.test.tsx` 以合成的同源 HTTP、WS、SSE 响应验证浏览器适配层，并覆盖 W6 WAL 中混合永续事件的查询水位。 |
| 期权敞口组件 | finance `option_exposure` 使用固定合约向量复算 GEX、wall、0DTE。frontend `options-u2b.test.tsx` 用本地合成数据验证敞口、观察和核验视图。生产期权上下游接线待后续批次。 |
| F12 期权历史 | `tests/finance-f12.sh` 校验 finance checkout 与 `HEAD:finance` 一致，从该 gitlink 的独立归档执行 `option_history` 及 `verified_paper_option_history_requests_and_decodes_synthetic_bars`。回归检查确认暂存的 pin 和未提交文件均无法改变测试源码，并拒绝不匹配的 checkout。用例覆盖未到期合约的只读窗口、权限、精确价量、到期零请求及 OPRA 离线估算。 |
| W3 邮件 worker 组件链 | web 的 `notifications` 库及 API 测试覆盖合成事件写入、去重、outbox、超时重试、静默、限流和 SMTP 恢复。`postgres_rule_silence_records_events_and_summarizes_after_release` 调用 `record_signal`，经临时 PostgreSQL 的 outbox 验证 worker 及 SMTP 替身。另有预置 outbox 的重试用例。生产业务事件接入 outbox 待后续批次。 |
| F3 WAL 缺口与重置 | `market_ingress` 检查缺口报告、恢复分类及重启，`market_wal_projector` 检查持久水位、封段与投影。隔离恢复核对保留的合成 gap、reset 与新 epoch。断源补数后在 web 和浏览器展示质量待后续接线。 |
| T2b 全量与增量恢复 | `test_encrypted_wal_full_and_hourly_restore_gap_and_new_epoch` 从合成 WAL 发布 `rclone crypt` 本地全量和小时增量，在空目标核对父链、增量封段、恢复水位及恢复事件。 |
| 旧回测与迁移 | 现有冒烟继续经 web 提交旧示例与研究 fixture，确认 worker 结果，并检查旧深链返回的 SPA 入口。`published_queue_readback.py` 用两个制品读取扩展后的合成队列。`web-database.py` 检查旧库升级、完整迁移台账、数据库权限和恢复。 |
| PR 门禁与质量响应 | `tests/t3a_contract_test.py` 执行文档门禁，确认只写 `Refs #44` 会失败。W6 将质量水位与源序号导出为 `QualityWatermarks` 和 `QualitySourceObservation`，质量接口响应字段为 `local_durable`、`projected`、`remote_verified`、`source_latest`。web 的 `quality_publish` 用合成输入验证真实发布命令和 HTTP router，frontend 的 `integrations-web.test.tsx` 检查精确的水位与源观测文本。 |

独立复验先按 [契约验证](../contracts/README.md#验证)从干净克隆检查 gitlinks 与生成物，再执行 [Integration](../.github/workflows/integration.yml) 和 [Recovery](../.github/workflows/recovery.yml) 的隔离用例。保留可公开的测试状态和计数。数据正文、邮件正文、数据库、日志和截图留在本地临时目录，处理边界见[四仓数据政策](data-policy.md)。

## 部署入口与升级路径

- 市场 WebSocket 的代理升级头和 `Origin` 转发核对只适用于前置反向代理。直连 web 的回环监听端口时，核对本机 ready、系统状态及鉴权后的市场流握手、收帧和重连。[WebSocket 入口核对](../ops/ruler-runbook.md#websocket-入口核对)
- 除[单进程热升级](../deploy/hot-upgrade.md#升级与兼容回退)外，维护窗口可走受监管启停与手动排空。暂停新回测提交，核对 `queued`、`running` 及 F13 租约归零并确认无在途引擎子进程后，再由守护器停止 worker。含新增迁移的发布在新版业务进程启动前单独执行 `web migrate` 并核对 SQLx 台账，回退按下节隔离执行。换版后核对持久队列、采集水位和入口就绪，再恢复接入。[受监管启停与手动排空](../ops/ruler-runbook.md#受监管启停与手动排空)
- `FERTILE_ENABLE_WEB_NOTIFIER=0` 时守护器不启动 `web-notifier`。核对 `disabled` 与 PID `0`。W11 的直接启动安全空转是 web 新版二进制的行为。[邮件运行时核对](../ops/ruler-runbook.md#v1-t3a-首批运行时核对)

## 含迁移发布的回退

回退前暂停 notifier 并清点 outbox。将迁移前备份恢复到新建的 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，在这对目标上运行旧发布。`<sha>` 为升级前的 fertile 提交号。旧代码不得连接新版写过的数据库，原数据根、数据库和台账留给新版。切回新版时按部署记录恢复原指针，将隔离窗口的写入另存并对账。[Ruler 回退步骤](../ops/ruler-runbook.md#从升级前快照回滚并切回新版) · [热回退适用范围](../deploy/hot-upgrade.md#升级与兼容回退)

supervisord 回退期间的启动和巡检须按 `migration-rollback.sha` 重建隔离参数。[Ruler 重启检查](../ops/ruler-runbook.md#重启与外部入口)

## 后续批次

- **质量接口。** 断源、补数与浏览器质量报告贯通验收随上游接线推进。
- **期权部署接线。** T3b 的期权 HTTP 路由和 U7 页面已接入。共享行情所有者的部署接线、实际纸户权限、深度 focus 到行情所有者的联动及期权流主动推送和续传待后续交付。
- **业务邮件与私有部署。** 生产业务事件入 outbox 的接线待后续批次。SMTP/Drive 账户、容量、收件人、离线密钥和行情许可由 vira 确认。外部备份、Binance 与 notifier 保持默认关闭。
