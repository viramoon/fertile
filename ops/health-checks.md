# 服务健康检查

从源码根运行 scripts/fertile status --json，记录部署提交、UTC 时间、进程 PID 与下面的业务探测。HTTP 状态为 200 表示对应探测通过，503 要结合响应里的 checks 定位。

| 服务 | 存活检查 | 就绪或业务检查 |
| --- | --- | --- |
| postgres | supervisor 和 pg_isready | Unix socket 连接数据库 web，读取 app.schema_info |
| finance-api | 8081 /healthz/live | 8081 /healthz/ready 检查数据根可读，/api/v1/market/status 提供独立的行情摘要 |
| finance-collector | 8082 /healthz/live | 8082 /healthz/ready 要求数据源、补数、catalog 写入和各标的新鲜度通过 |
| finance-backtest-worker | supervisor 的 worker PID | 经 web 提交带幂等键的回测，轮询同一 ID 到 succeeded |
| web | 8090 /healthz/live | /healthz/ready 检查数据库、静态首页和 finance；默认 finance 中断返回 degraded，/api/v1/system/status 提供分项结果 |
| frontend | web 的首页及带摘要的 assets | 浏览器深链刷新成功，读取状态与示例回测 |
| 外部 HTTPS | Ruler 管理 | https://fertile.viramoon.com/healthz/ready；与本机结果分别记录 |
| ib-gateway | 本机 4002 监听检查 | finance-ib probe 完成握手、取时间并验证 DU 账户；账户标识在日志中脱敏 |

```sh
curl -fsS http://127.0.0.1:8081/healthz/ready
curl -sS -w ' HTTP %{http_code}\n' http://127.0.0.1:8082/healthz/ready
curl -fsS http://127.0.0.1:8090/api/v1/system/status
curl -fsS http://127.0.0.1:8090/api/v1/backtests/sample
```

Gateway 缺凭据时先处理 needs_credentials。行情陈旧时核对数据源、交易时段、最后 bar、水位和补数日志。网页可用与 collector 就绪分别记录；交易开放另受风控与对账约束。

巡检进程把采样写入 logs/healthwatch/samples.jsonl，故障和恢复写入 logs/alerts/alerts.jsonl。核对同一服务先出现故障、随后恢复，并保留起止时间。公网 /healthz/ready 独立于本机 web 的 200；在 public_origin 确认前检查显示跳过。备份年龄从已完成且有校验清单的快照读取。详见 [巡检与告警](monitoring.md)。
