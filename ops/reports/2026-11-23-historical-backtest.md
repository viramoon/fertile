# 历史回测候选验收记录

2026-11-23 UTC。独立 Ubuntu 容器和持久测试卷，box 用户运行，PostgreSQL 17.12。候选代码为 e9f25ad97f2ec641ce748d89b6ae9459671f3485，组件锁定在 finance 0305dc4、web 42dce4d、frontend 932c1e6。行情用 ReplaySource，网络演练单独切换到回环 IB 协议替身。实际目标机器、线上站点、共享隧道、DNS 与真实 paper 由 Ruler 验收。

## 构建、数据和网页

~~~text
bash scripts/build.sh（finance，锁定依赖）
BUILD_CACHE_HIT=false; BUILD_SECONDS=2397
SOURCE_COMMIT=27a8e54e7ac3c57d6cbf8891f0283dfbaee97317
NAUTILUS_VERSION=1.231.0

scripts/fertile build --ref e9f25ad97f2ec641ce748d89b6ae9459671f3485
源码 wheel SHA 通过；Rust release、Vite 产物、已验证当前目录
scripts/fertile ensure-up
web 127.0.0.1:8090、finance 8081、collector 8082 均就绪 200
WEB_PORT=8080 scripts/fertile ensure-up 拒绝，原 web PID 92 保持
单独测试 HTTP 服务在 8080 的响应仍为 isolated-existing-site-on-8080

tests/e2e-smoke.sh local --research-instrument IBM.XNYS --research-kind historical
202 受理，GET /api/v1/backtests/by-key 200；重复 POST 仍返回 id=2
succeeded; 2024-01-02 至 2025-12-31，502 个交易日，22 笔审计成交
1125.00 USD 模拟盈亏，50、10、5 年窗口覆盖均不足

tests/e2e-smoke.sh local --research-instrument FIXTURE.XNYS --research-kind fixture
id=4 succeeded; 35 个合成交易日，2 笔模拟成交，1 根零量日线保留
~~~

IBM 数据为 Yahoo Finance 公开 quote OHLCV，原始响应 SHA-256 为 e0d26b93e263259c591befc5a0f08ae5a6c80014660ca79cb969372499c6f2e5。核对历史区间的 8 条现金分红和零拆股记录，价格对齐美分；入库 CSV SHA-256 为 ca333239fc4eca8699a6eac08a82de366db2e9fffcbbe7e0b819de432211652d。来源文件留在测试卷。作业报告将现金分红排除，披露 warmup、净值起点、佣金、滑点、RF 与覆盖。模拟结果独立于真实账户收益。

### Chromium

以现有隔离 Chromium 环境经本机 TCP 和 Unix socket 转接到上述 8090 实例。实际浏览器填写 IBM、日期、均线和测试令牌。测试转发首次真实 POST，等后端返回 202 后丢弃响应。浏览器通过原幂等键取回 job=6，整个过程只有一次 POST。页面展示 502 个交易日和覆盖不足，断网页面提示、重连刷新、390px 宽度和仪表盘的 Replay 行情状态均通过。令牌从表单清空，未写入浏览器存储。

## 持久作业与恢复

~~~text
SIGKILL supervised historical worker pid=1568 with job=5 running attempt=1
worker pid=1948; same historical job=5 attempt=2 status=succeeded
5.269s, 502 bars, same key

python3 tests/backup_restore.py -v
PostgreSQL 17.12 / 2 tests passed
SIGKILL after rename recovered; damaged SHA256 refused before writes
SQLite WAL, Parquet and research provenance restored
legacy snapshot preserved research catalog
~~~

备份和恢复命令用真实 PostgreSQL、SQLite 和 Parquet。最终候选的快照也通过备份清单与 PostgreSQL 归档校验。

### 参数拒绝、断网与恢复

~~~text
真实 HTTP 回测提交
unknown strategy: HTTP 422 / invalid_request
missing dataset: HTTP 422 / no_data
pre-epoch start: HTTP 422 / invalid_request
job 7: failed / insufficient_history; no sample or partial result

tests/chaos/network.sh --protocol-fixture --duration 120 --fixture-origin https://127.0.0.1:14443
network_fault_cleared duration_seconds=120.04
First collector HTTP 503 after 3.023s during the outage
fixture_origin_ready_seconds=1.05, collector_ready_seconds=27.34
Independent collector check: filled_minutes=2, ready in 25.14s
post_outage_watermark_seconds=57.66, 4 unique SPY.ARCA minutes read from Nautilus
public:ready, service:finance-collector:ready, market:fresh logged failure and recovery
network_drill_complete; actual HTTP smoke job succeeded after recovery
~~~

故障注入仅作用于本次隔离容器的本地 IB 协议和自建 TLS 入口。财务服务验证实际 HTTP、PostgreSQL、采集器与 Nautilus。真实 Paper、真实交易所会话及生产入口另需 Ruler 按手册验收。演练结束将行情切回 Replay，并恢复独立测试数据根。

### 进程、数据库与进程树恢复

~~~text
python3 tests/chaos/services.py services
SIGKILL postgres 3002→3124 1.247s
SIGKILL finance-api 3018→3218 1.246s
SIGKILL finance-collector 3021→3297 2.517s
SIGKILL finance-backtest-worker 3028→3405 1.191s
SIGKILL web 3046→3481 1.266s
SIGKILL healthwatch 3051→3560 1.541s
每次自动拉起后，8090 上的实际 HTTP 冒烟均成功

python3 tests/chaos/services.py postgres
正常停库耗时 0.215s，web ready=503，finance ready=200
重连耗时 1.793s，web PID 3481 和 finance PID 3218 保持，冒烟成功

python3 tests/chaos/services.py reboot
SIGKILL 独立主管进程树，fertile ensure 在 8.211s 后恢复全部启用进程
主管 46→3744，web、finance 和采集器 HTTP 就绪均 200，冒烟成功
8080 响应保持 isolated-existing-site-on-8080
~~~

### 回滚与整容器恢复

~~~text
scripts/fertile rollback b7d8dc2362da6d4ca794fe140fc3d8f3bdcef502
fertile: This bundle cannot read the existing research queue. Retain it and select a compatible data snapshot.
FERTILE_DATA_ROOT=/home/box/fertile/data/replay-legacy scripts/fertile rollback b7d8dc2362da6d4ca794fe140fc3d8f3bdcef502
旧构建保留 8090，独立兼容队列上的 HTTP 冒烟通过
scripts/fertile rollback e9f25ad97f2ec641ce748d89b6ae9459671f3485
当前队列全部作业的 HTTP 列表摘要相同，8080 测试网站保持原响应

重建唯一的隔离容器，保留挂载的 /home/box
isolated container replaced: 4955a0d52563 -> 89eb3aead9e3
scripts/fertile ensure-up
historical job 2 retained, HTTP payload SHA-256:
320bee89b4ab4c05e6f6df63d7c4d86f5a2e74f37e9f83b1a3c93a25fb3d185e
tests/e2e-smoke.sh local --research-instrument IBM.XNYS --research-kind historical
job 17 succeeded, 502 trading sessions, 22 audited fills, all 50/10/5 insufficient
~~~

## 验收环境与状态

| 场景 | 状态 |
| --- | --- |
| 离线 fixture | 通过。实际 8090、PostgreSQL、worker、合成日线 Parquet 和本地 IB/TLS 协议替身。 |
| 真实历史数据 | 通过。真实报价导入独立 Nautilus catalog，真实浏览器完成提交与取回结果。 |
| 实际 IBKR paper | 待 Ruler 在目标盒子完成本地 Gateway、2FA、开市行情和 120 秒回补。 |
| 公网 | 待 Ruler 配置并验收 fertile.viramoon.com 的 DNS、Tunnel 和 HTTPS。 |
| 目标盒子恢复 | 待 Ruler 在维护窗口复验。隔离进程树重启、容器重建与 PostgreSQL 数据恢复已实测。 |
| 隔离候选的 24 小时观察 | 持续采样中。2026-11-23 12:43:39 至 2026-11-24 12:43:39（北京时间），ReplaySource，生产公网探针关闭。 |
| 目标盒子的 24 小时观察 | 待 Ruler 在目标部署采集真实 paper 和公网样本。 |

复现命令和目标环境的故障窗口安排见 [Maas · Ruler 云盒子操作手册](../ruler-runbook.md)。
