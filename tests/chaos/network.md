# Paper 行情与 Tunnel 断网演练

`tests/chaos/network.sh` 在已运行的部署上阻断本机 IPv4 loopback 发往 TCP 4002 的流量，同时暂停该实例由 supervisord 管理的 QUIC cloudflared 进程。默认持续 120 秒，支持 120 至 300 秒。命令不会读取或修改 Cloudflare 凭据，不会修改主机默认防火墙策略。演练期间公网入口暂时不可用；同机其他连接 127.0.0.1:4002 的 paper 客户端也会受影响。

## 在目标盒子执行

使用部署账户，从含当前版本测试的 fertile checkout 运行。环境需要 Linux pidfd、iptables 及直接操作指定网络命名空间的权限，或针对 iptables 的非交互式 sudo 权限。确认运行的是本机只读 paper Gateway、实时 IB 分钟线 collector 和默认服务清单，告警只写本地文件。先检查 `scripts/fertile status --json`、`scripts/ib-check.sh` 和 [巡检规则](../../ops/monitoring.md)。同一告警类型有 15 分钟去重窗口，等近期故障完全恢复并过了该窗口再安排下一次演练。

公网完整模式要求操作员明确提供已确认的 HTTPS origin，且它与当前 healthwatch 配置一致。候选域名由使用者确定。脚本先核实公网与本机返回相同的 web 提交和启动时间，验证 supervisor 父子关系、cloudflared 可执行文件和 QUIC 参数，再用 pidfd 固定进程身份。

```bash
source /home/box/fertile/env.sh
set -o pipefail
mkdir -p "$FERTILE_ROOT/logs/drills"
tests/chaos/network.sh --duration 120 --public-origin "$FERTILE_PUBLIC_ORIGIN" \
  | tee "$FERTILE_ROOT/logs/drills/network-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
```

实际 IB 模式只在美国股票常规交易时段运行。脚本用固定版本的 `exchange-calendars` 查询 XNYS 开、收盘时间，排除节假日和提前收盘后的时段，并要求恢复验证有足够时间。第一次使用时 uv 会缓存独立的日历依赖。可事先执行 `uv run --script tests/chaos/network-session.py "$(date -u +%F)"`。临时休市、停牌和分钟内无成交会影响验收，应选取正常连续交易的高流动性标的。可用 `--session-open` 与 `--session-close`（带 UTC 偏移的 ISO 时间）进一步缩小演练窗口。

## 验收证据

- 前置检查要求本地各核心服务 ready，Gateway 握手返回 DU 前缀和当前服务器时间，finance 报告 `ib-realtime` 且每个标的新鲜。校验运行中配置、catalog 的来源标记和 collector 的磁盘水位，并通过 release 自带的 Nautilus wheel 读取起始分钟。
- `network_fault_active` 之后，实际连接 127.0.0.1:4002 必须超时或失败，collector 的 ready 必须出现 503，公网 ready 必须出现故障。当前演练只添加一条带随机标识的 OUTPUT 规则。控制进程退出时，管道关闭触发清理。辅助进程还持有时限清理，Tunnel 另由独立进程按 pidfd 恢复。用 `SIGINT` 可以提前终止和清理；未完成整个阻断窗口会得到非零结果。
- `network_fault_cleared` 记录实测阻断时长。随后再次握手，要求同一个 collector 进程重新 ready。巡检 JSONL 须包含此次断网窗口内的行情或 collector 故障，公众入口故障需要单独记录，两者随后均应解除。
- 验收持续到每个标的的水位超过解除后的第一个整分钟，并用 Nautilus 回读从原水位到新水位的整个区间。每个整分钟都应恰好有一根已收盘的 bar。缺失或重复一根都会令演练失败。
- `network_recovery_verified` 记录行情、公网、恢复后水位三个阶段的秒数、告警 UTC 时间和每个标的的分钟数。最后重跑本地冒烟与同一已确认域名的公网冒烟，包含真实的回测作业。只有两次均成功才产生完整模式的 `network_drill_complete`。

恢复失败时保留日志并检查 `iptables -S OUTPUT` 中本次 `fertile-drill-` 规则、`scripts/fertile status --json`、`scripts/ib-check.sh` 和 alerts.jsonl。不要清空 OUTPUT 链。若 Gateway 报人工确认或行情仍过期，在恢复真实行情和补数前保留该项失败。

## 受限环境

- `tests/chaos/network.sh --paper-only --duration 120` 仍要求真实 paper 握手、交易时段、补数、告警和本地冒烟。该模式用于未配置公网 origin 的部署，跳过 Tunnel 和公网两项，输出列出未验证项。
- `--protocol-fixture` 用于独立的开发或 CI 网络命名空间。真实 Gateway、Xvfb 和 cloudflared 均须显式禁用，测试环境无 IBKR、Cloudflare 和 webhook 凭据。`FERTILE_SMOKE_FIXTURE_PID` 必须是持有 127.0.0.1:4002 监听 socket 的测试 peer。该模式使用真实应用、iptables、告警和 Parquet，同时跳过真实账户、交易所会话、Cloudflare 和公网冒烟。可向 `--fixture-origin` 提供 **127.0.0.1 的 HTTPS 地址** 验证本地 TLS 故障与恢复；由隔离夹具响应 active/cleared 两个事件暂停和恢复该监听，healthwatch 必须配置同一地址。全程不会向真实域名发送请求。
- 轻量测试可运行 `python3 tests/chaos/network_test.py`。它在私有子进程上验证控制进程收到 SIGKILL 后，守护进程仍会恢复被暂停的目标。实际盒子验收请保存完整模式 JSONL 及两个 smoke 的退出码。
