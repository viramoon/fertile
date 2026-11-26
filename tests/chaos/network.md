# Paper 行情断网演练

在 Ruler 批准的维护窗口，确认只有本次演练的客户端使用本机 paper 端口。脚本验证部署身份，然后在当前网络命名空间插入限定于本机 IPv4 loopback、目标 TCP 4002 的临时 DROP。持续 120 至 300 秒。真实账户保持 paper 只读。保留本机管理通道和告警日志。

脚本验证同一个 collector 先失去就绪、后重订阅并补齐连续分钟线，同时核对合成试验或维护窗口的告警与恢复。前后用实际 Parquet 读回验证时间戳唯一。临时规则带独立标签，正常结束或控制器退出后由守护清除。不会清空防火墙规则。

## 本地压力测试机

本机只做本地部署复验与压力测试，网络演练默认只发起本机检查。`--paper-only` 也可显式要求本地模式。执行纸面行情断网演练时，确认 Gateway 已登录、会话身份为 DU，官方交易日历处于开盘时段且距收盘足够久。若 SaaS 或代理的长连接周期性中断，另记录断线频率、reconnect/backoff、恢复水位和失败区间。

~~~sh
source /home/box/fertile/env.sh
scripts/ib-check.sh
scripts/fertile status --json
tests/e2e-smoke.sh local --paper-only
tests/chaos/network.sh --duration 120
~~~

演练本身要求实时 paper 行情和初始服务就绪，恢复后会运行完整的本地冒烟。

## 未来生产服务器的可选公网校验

由运维为该服务器显式设置 `FERTILE_PUBLIC_ORIGIN` 或在巡检覆盖文件设置 `public_origin` 后，使用匹配的 HTTPS origin 运行以下命令。脚本比对本地与公网 web 进程身份，分别检查 HTTPS 就绪和冒烟。故障只对本机 paper 连接施加。

~~~sh
tests/chaos/network.sh --public-origin 'https://<your-origin>' --duration 120
~~~

## 隔离协议试验

隔离网络命名空间中运行真实业务进程与 TCP 协议测试端。真实 Gateway 和 Xvfb 必须禁用，须提供拥有 127.0.0.1:4002 监听 socket 的测试端 PID。可选本机 HTTPS 测试入口由该命名空间的试验驱动在 active/cleared 事件时断开和恢复。巡检配置与 fixture origin 一致。此模式验证内核断线、真实重连、告警转换和 Parquet 回补。报告标记协议替身与本机 TLS。

~~~sh
export FERTILE_SMOKE_FIXTURE_PID="$local_test_peer_pid"
tests/chaos/network.sh --protocol-fixture --duration 120 --fixture-origin https://127.0.0.1:14443
python3 tests/chaos/network_test.py -v
~~~
