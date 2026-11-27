# v1 T3 首批集成与隔离验收

本批关联 [fertile #44](https://github.com/viramoon/fertile/issues/44)，使用 `Refs #44`。最终验收随子仓契约分批推进。部署方按 [Ruler 手册](../ops/ruler-runbook.md#v1-t3-首批运行时核对)核对制品、迁移和可选进程。

## 版本与契约

| 子仓 | 本批 gitlink | 消费方记录 |
| --- | --- | --- |
| finance | `19291f8a969266a4cc67e7d7a7f676754a87b839` | web 记录 `617ed83`。这两个版本导出相同的 OpenAPI，版本化协议及读流导出一致。 |
| web | `9132c5dc2e40f94d96151fbcc2989abbbb53d778` | frontend 仍用 `d9e5c79` 对应的 W4 契约。当前 web 已含 W5 与 W2a，两个阶段的 OpenAPI 分别验证。 |
| frontend | `3788ae986ea61762b4219fd17547b5e2840c93cb` | U2a、U2b、U3、U4 已合并。U5 更新生成客户端并接入 W2a 的生产数据源。 |

finance 在 `19291f8` 之后由 F6 扩展 bars 与指标查询，当前候选 `fc5e153` 又含后续协议变动。首批使用与 web 输入一致的最新 main 版本。推进时先合并 finance 契约并更新 web 对应快照和生成客户端，再由 frontend 更新 web 输入，最后调整总仓 gitlink。[契约校验和导出流程](../contracts/README.md)

finance 读接口增加 WAL 产品、水位、事件、质量、最新投影及因子研究查询。价格、数量、序号与纳秒时钟沿用契约定义的精确十进制字符串。web 新增行情、自选、推送、集成测试和通知接口。`/api/v1/stream/market` 与 `/api/v1/events` 复用 web 的现有 HTTP 入口，且分别使用 WebSocket 与 SSE。期权查询返回 `finance_contract_pending`。生产接口细节见 [finance API](https://github.com/viramoon/finance/blob/19291f8a969266a4cc67e7d7a7f676754a87b839/docs/api.md) 与 [web API](https://github.com/viramoon/web/blob/9132c5dc2e40f94d96151fbcc2989abbbb53d778/docs/api.md)。

## 合成验收

CI 的 Integration 作业从本批 gitlink 构建制品。九只股票的回放 CSV 由 `tests/replay_fixture.py` 在临时目录生成，标的及价量使用固定向量，价量由整数计算，运行时钟由 ReplaySource 处理。测试全程使用合成数据、回放或本地替身。测试目录、队列和数据库均从空隔离实例创建，IBKR 仅以合成回放或内存 paper 只读替身验证。

| 验收项目 | 路径与判定 |
| --- | --- |
| 九标的 | `tests/e2e-smoke.sh local --replay-nine` 在隔离 ReplaySource 实例检查精确标的集合及每个标的新鲜已落盘 bar。覆盖 QQQ、SPY、AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA。前端在 U5 合并前以自身固定合成用例验证切换。 |
| 期权敞口 | finance `option_exposure` 使用固定合约向量复算 GEX、wall、0DTE。frontend `options-u2b.test.tsx` 用合成数据验证敞口、观察和核验的展示边界。F9e 观察/核验证据的生产链仍待合并。 |
| F12 历史期权 | 首批 gitlink 尚未包含 F12。在 finance `f4786109e3812e4946adb92e97e513f1fbae5d79` 的独立归档和 Cargo target 中执行 `option_history` 及 `verified_paper_option_history_requests_and_decodes_synthetic_bars`。覆盖未到期合约的只读窗口、权限、精确价量、到期零请求及 OPRA 离线估算。 |
| W3 邮件 | web 的 `notifications` 库及 API 测试覆盖合成事件去重、outbox、超时重试、静默/限流与 SMTP 恢复。邮件传输使用测试替身，outbox 使用临时本地 PostgreSQL。 |
| F3 缺口与 T2b 恢复 | `market_ingress` 检查缺口报告与恢复分类，`market_wal_projector` 检查持久水位、封段与投影。`test_encrypted_wal_full_and_hourly_restore_gap_and_new_epoch` 从合成 WAL 发布 `rclone crypt` 本地全量和小时增量，在空目标验证各自的恢复水位及跨代际缺口保留。 |
| 旧回测与迁移 | 现有冒烟继续经 web 提交旧示例与研究 fixture，确认 worker 结果及旧深链。`published_queue_readback.py` 用两个制品读取扩展后的合成队列。`web-database.py` 检查旧库升级、完整迁移台账、数据库权限和恢复。 |

独立复验先按 [契约验证](../contracts/README.md#验证)从干净克隆检查 gitlinks 与生成物，再执行 [Integration](../.github/workflows/integration.yml) 和 [Recovery](../.github/workflows/recovery.yml) 的隔离用例。保留可公开的测试状态和计数。数据正文、邮件正文、数据库、日志和截图留在本地临时目录，处理边界见[四仓数据政策](data-policy.md)。

## 后续批次

- U5 合并到 frontend main 后，更新其 gitlink，以 `9132c5d` 为 web 契约基线重新生成并复验 W2a 接线和九标的终端路径。
- W2a 的 OpenAPI 注册覆盖了 W4 运维状态接口同名的 `MarketWatermarks` 和 `SourceObservation` schema。W4 的实际响应仍使用原字段。前移 U5 前核对生成类型及 `/api/v1/quality/gaps` 的实际响应，所需 schema 修复由 web 通过导出流程交付。
- finance 与 web 对齐后续 F10，再把 F12 推入总仓候选。F9e 期权观察/核验及 F10 后续读契约、W2b 策略研究网关仍需各发布端合并及端到端回放。
- F9b 行数调度和 F13 进程拆分位于首批 finance gitlink 之后，部署继续按当前进程清单选择已提供的能力。
- 外部 SMTP/Drive 账户、容量、收件人、离线密钥和行情许可由 vira 在私有部署确认。外部备份、Binance 与 notifier 维持各自默认关闭设置。
