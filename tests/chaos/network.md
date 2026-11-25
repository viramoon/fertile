# Paper 行情断网演练

在 Ruler 批准的维护窗口，确认只有本次演练的客户端使用本机 paper 端口。脚本验证部署身份，然后在当前网络命名空间插入限定于本机 IPv4 loopback、目标 TCP 4002 的临时 DROP。持续 120 至 300 秒。真实账户保持 paper 只读。保留本机管理通道和告警日志。

脚本验证同一个 collector 先失去就绪、后重订阅并补齐连续分钟线，同时核对合成试验或维护窗口的告警与恢复。前后用实际 Parquet 读回验证时间戳唯一。临时规则带独立标签，正常结束或控制器退出后由守护清除。不会清空防火墙规则。

## 目标盒子

确认 Gateway 已登录、会话身份为 DU，官方交易日历处于开盘时段且距收盘足够久。先跑完整本地冒烟。若 SaaS 或代理的长连接周期性中断，另记录断线频率、reconnect/backoff、恢复水位和失败区间。

~~~sh
source /home/box/fertile/env.sh
scripts/ib-check.sh
scripts/fertile status --json
tests/e2e-smoke.sh local
tests/chaos/network.sh --paper-only --duration 120
~~~

Ruler 完成 fertile.viramoon.com → 127.0.0.1:8090 的路由后，可以在同一演练中独立校验公网入口。脚本比对本地与公网 web 身份，检查 HTTPS 就绪，故障只对 paper 连接施加。共享站点、隧道进程、隧道凭据、DNS 和现场恢复仍由 Ruler 管理。

~~~sh
tests/chaos/network.sh --public-origin https://fertile.viramoon.com --duration 120
~~~

## 隔离协议试验

隔离网络命名空间中运行真实业务进程与 TCP 协议测试端。真实 Gateway 和 Xvfb 必须禁用，须提供拥有 127.0.0.1:4002 监听 socket 的测试端 PID。可选本机 HTTPS 测试入口由该命名空间的试验驱动在 active/cleared 事件时断开和恢复。巡检配置与 fixture origin 一致。此模式验证内核断线、真实重连、告警转换和 Parquet 回补。报告标记协议替身与本机 TLS。

~~~sh
export FERTILE_SMOKE_FIXTURE_PID="$local_test_peer_pid"
tests/chaos/network.sh --protocol-fixture --duration 120 --fixture-origin https://127.0.0.1:14443
python3 tests/chaos/network_test.py -v
~~~
