# 健康巡检、告警与观察报告

[healthwatch](../scripts/healthwatch) 由 supervisord 守护，默认每 30 秒采样一次。所有时间使用 UTC。默认根目录为 `/home/box/fertile`，演练时设置 `FERTILE_ROOT` 可完全隔离运行目录、日志和备份。

| 文件 | 内容 |
| --- | --- |
| `logs/healthwatch/samples.jsonl` | 每轮探针的 ok、failed 或 skipped、原因及指标，含 profile、行情源、部署提交与采样间隔 |
| `logs/alerts/alerts.jsonl` | 各探针首次故障与恢复事件，含固定事件 ID |
| `logs/healthwatch/stdout.log` | supervisord 捕获的启动、配置和写盘错误 |
| `logs/healthwatch/backup.log` | 定时备份命令的输出 |
| `run/healthwatch-webhook.cursor` | 已投递或默认关闭通道期间略过的通知位置 |

## 探针与状态变化

- finance-api、finance-collector、web 的本地 live 和 ready 各有单独探针。服务清单来自 `scripts/fertile status --json`，启用项另查 supervisor 进程状态；postgres、worker、IB Gateway 等没有 HTTP 端点的服务使用进程探针。手动禁用与 replay 隐含禁用写 skipped；已启用服务缺程序或缺登录凭据记录 failed。
- 公网独立请求 https://fertile.viramoon.com 的 `/healthz/ready`，要求 HTTP 200 和有效的 web 就绪 JSON。必需依赖均健康时，web 对可选依赖的降级状态仍视为入口可达。跳转、非预期 HTML、TLS 错误及 Tunnel 502/530 使该探针失败。隔离实例显式设置 `public_origin: null` 后，该探针记录 skipped。检查从盒子访问公网入口；不同运营商与地域的可达性另需从相应地点测量。
- `market:fresh` 从 finance `/api/v1/market/status` 读取业务状态、每个标的的年龄和陈旧标志。统计标的数、陈旧数、观测到的最大年龄及数据源；缺少行情或数据心跳过期标记失败。未启用采集或旧版接口返回 404 会记录 skipped。
- 磁盘检查默认为部署根的父目录，对应 `/home/box`。可用空间低于 2 GiB 时告警。最近完成的备份需同时具备合法 UTC 时间戳目录、`snapshot.json` 和 `SHA256SUMS`；超过 36 小时、时间在未来或从未完成均告警。清单内容完整性由备份及恢复流程校验。
- 未就绪、已断开的服务与行情陈旧分别计时。业务健康不等同于 PID 存在；交易的订单核对、未决写请求和真实券商认证应结合 [健康检查](health-checks.md) 与 [Ruler 手册](ruler-runbook.md) 验证。采样不能发现两次轮询之间始末皆已结束的短暂故障，系统与守护日志可补充事件级时间线。

持续故障只写一条 failure，恢复写一条 recovery。短时抖动对同一探针、同一方向设 15 分钟冷却；冷却期内的样本仍完整保留，仍未恢复的第二次故障会在冷却结束后通知。重启从告警日志恢复去重状态。一个部署根同时只允许一个采样进程。巡检自己的异常保留在运行或服务日志；采样停止或写盘失败可在报告中识别为缺口。

## 运行与配置

在部署环境中安装控制端 Python 依赖并运行 `scripts/fertile ensure-up`。默认设置在 [deploy/healthwatch.yaml](../deploy/healthwatch.yaml)，实例级覆盖可在 `config/runtime.env` 设置 `FERTILE_HEALTH_CONFIG=/home/box/fertile/config/healthwatch.yaml`。覆盖文件接受同名配置、`disk_path`、`public_origin`。公网来源默认继承 [服务部署清单](../deploy/service.yaml)，显式设置 `FERTILE_PUBLIC_ORIGIN` 优先。配置更改后重启 healthwatch 并核对采样结果。

```sh
root=${FERTILE_ROOT:-/home/box/fertile}
scripts/fertile status --json
tail -f "$root/logs/alerts/alerts.jsonl"
tail -1 "$root/logs/healthwatch/samples.jsonl" | python3 -m json.tool

# 仅在此实例没有运行常驻 healthwatch 时进行单次采样。
scripts/healthwatch --once
```

`--once` 写入一次测量日志，以 JSON 输出检查结果，跳过定时备份调度。通过 stdout 中每个探针的 state 判断依赖健康。常驻模式默认每天 03:00 UTC 异步调用 [backup.sh](../scripts/backup.sh)，错过计划时刻会在当天首轮补跑；失败在 15 分钟后重试。可设置 `backup_at_utc: "HH:MM"` 调整时刻，设为 null 停用调度。可在 runtime.env 配置 FERTILE_BACKUP_DAYS（默认 7）、FERTILE_BACKUP_WEEKS（默认 4）和可选 FERTILE_BACKUP_REMOTE（仅 rclone 目标名，凭据另行管理）。备份前置锁、校验和恢复流程见 [Ruler 手册](ruler-runbook.md)。

## 可选 webhook

默认只记本地告警。经运维确认后可在根目录的 `secrets/alerts.env` 配置单行 `ALERT_WEBHOOK_URL`；文件必须由运行账号所有、权限为 0600 的常规文件，URL 必须使用 HTTPS。独立测试仅投递至本地 TLS 接收端。不要将地址、token、账户或密码提交到 Git。

推送体为对应的 JSON 告警，事件 ID 同时置于 `Idempotency-Key` 请求头。发送失败保留游标，后续重试，并由 `webhook:delivery` 本地记录投递故障。网络中断或进程在服务端接收后、写入游标前退出时，接收端可据相同事件 ID 去重。关闭通道期间记录的事件不在日后集中重发。归档日志前保留原始样本与告警的匹配时间窗，避免报告丢失故障起点。

## 24 小时观察

在完整部署启动后记录观察开始时刻和环境，持续采样实际墙钟 24 小时。用 [soak_report.py](../scripts/soak_report.py) 指定同一窗口及报告路径。下例创建当前部署的本地报告；提交前复核来源、域名和跨版本情况。

```sh
root=${FERTILE_ROOT:-/home/box/fertile}
mkdir -p ops/reports
python3 scripts/soak_report.py \
  --samples "$root/logs/healthwatch/samples.jsonl" \
  --alerts "$root/logs/alerts/alerts.jsonl" \
  --start "$OBSERVATION_START_UTC" --end "$OBSERVATION_END_UTC" \
  --label "$OBSERVATION_ENVIRONMENT" --require-hours 24 \
  --output "ops/reports/$(date -u +%F)-soak.md"
```

报告列出每项探针观测可用率、断线次数、恢复耗时、未恢复事件、行情最大陈旧年龄、跳过原因、采样缺口和实际告警。覆盖不足或存在缺测时仍生成报告，时长门禁以非零状态退出。应写明使用真实 paper 或 replay、实际公网来源，以及是否覆盖美股交易时段。隔离容器、回放源和测试 TLS 入口均按其实际环境标记。

现有网站、Cloudflare 和 DNS 由 Ruler 管理。healthwatch 只探测公网的 fertile 健康端点；本地隔离实例在巡检覆盖文件中使用 `public_origin: null`。部署脚本和巡检无需访问 Tunnel 凭据。
