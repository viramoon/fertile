# 服务健康检查

从源码根运行 `scripts/fertile status --json`，记录部署提交、UTC 时间、进程 PID 与下面的业务探测。状态命令识别当前持久根的已安装 systemd 实例，其他环境从 supervisord 取进程状态。HTTP 状态为 200 表示对应探测通过，503 要结合响应里的 checks 定位。[普通服务器部署指南](../deploy/linux-server.md)

`scripts/fertile status --json` 从控制端脚本所在的检出或发布目录读取 `VERSION`，仅请求清单中配置的探针 URL，发送 `fertile-supervision/<VERSION>` User-Agent，在报告中保留首跳 HTTP 状态码。运行目录中的服务启动器副本直接执行 `_exec`，版本读取留在控制端状态检查阶段。`tests/chaos-restart.sh` 的独立存活探针发送 `fertile-supervisor-drill/<VERSION>`，同样以配置的 URL 的首跳结果为准，遇到 3xx 会报告失败。

systemd 模式的 PostgreSQL unit 启动时等待 Unix socket 通过 `pg_isready`。状态命令读取 systemd 的服务 PID，并请求与 supervisord 相同的 HTTP live 和 ready 端点。核心五项的 systemd 安装未配置 healthwatch unit，连续采样由需要该功能的环境另行安排。

| 服务 | 存活检查 | 就绪或业务检查 |
| --- | --- | --- |
| postgres | 守护器进程状态和 pg_isready | Unix socket 连接数据库 web，读取 app.schema_info |
| finance-api | 8081 /healthz/live | 8081 /healthz/ready 检查数据根可读，/api/v1/market/status 提供独立的行情摘要 |
| finance-collector | 8082 /healthz/live | 8082 /healthz/ready 要求数据源、补数、catalog 写入和各标的新鲜度通过 |
| finance-backtest-worker | 受管 worker PID | 经 web 提交带幂等键的回测，轮询同一 ID 到 succeeded |
| web | 8090 /healthz/live | /healthz/ready 检查数据库、静态首页和 finance；默认 finance 中断返回 degraded，/api/v1/system/status 提供分项结果 |
| frontend | web 的首页及带摘要的 assets | 浏览器深链刷新成功，读取状态与示例回测 |
| 外部 HTTPS（仅未来生产服务器显式启用） | 运维配置 HTTPS origin | `https://<your-origin>/healthz/ready`，与本机结果分别记录 |
| ib-gateway | 本机 4002 监听检查 | finance-ib probe 完成握手、取时间并验证 DU 账户；账户标识在日志中脱敏 |

```sh
curl -fsS http://127.0.0.1:8081/healthz/ready
curl -sS -w ' HTTP %{http_code}\n' http://127.0.0.1:8082/healthz/ready
curl -fsS http://127.0.0.1:8090/api/v1/system/status
curl -fsS http://127.0.0.1:8090/api/v1/backtests/sample
```

Gateway 缺凭据时先处理 needs_credentials。行情陈旧时核对数据源、交易时段、最后 bar、水位和补数日志。网页可用与 collector 就绪分别记录；交易开放另受风控与对账约束。

巡检进程把采样写入 `logs/healthwatch/samples.jsonl`，故障和恢复写入 `logs/alerts/alerts.jsonl`。核对同一服务先出现故障、随后恢复，并保留起止时间。备份年龄从已完成且有校验清单的快照读取。本机用于本地压力测试，部署复验使用 `tests/e2e-smoke.sh local --paper-only`，公网巡检记录为跳过。未来生产服务器由运维显式启用后，公网 `/healthz/ready` 与本机 web 就绪分别采样。详见 [巡检与告警](monitoring.md)。
