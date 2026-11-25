# 隔离环境 24 小时观察验收

**通过。** 隔离实例在北京时间 2026-11-23 13:04:51.397（UTC+08:00）至 2026-11-24 13:04:51.397（UTC+08:00）完成 24 小时采样。按实际样本覆盖 86,400 秒，共 2,880 轮，采样缺口为零。以下巡检表和告警时间使用 UTC。

## 运行环境与探测对象

Ubuntu 24.04 容器 `ha-ingress-qa` 使用独立持久测试卷，运行配置为 `cloudbox`，行情源为 ReplaySource。整段采样只出现下列发布标识，三个 gitlink 与实际部署的 `current/manifest.json` 一致。

| 仓库 | 实测提交 |
| --- | --- |
| fertile | `afc4a5c655eced04c0ba2ff1ef7a90fdef675bac` |
| finance | `0305dc4aa518f398bea09ff82b0ebc07e09158e9` |
| web | `a5e41ee3d5404fbbf51381accc5e5e524b04b032` |
| frontend | `80cc07f77fdbf8c6ef9de91cd7d65488f4537e94` |

- 本机 HTTP 探针是 web `127.0.0.1:8090`、finance API `127.0.0.1:8081` 和采集器 `127.0.0.1:8082` 的 `/healthz/live` 与 `/healthz/ready`。巡检通过行情状态读取 ReplaySource 的新鲜度。
- 同时观测 supervisor、PostgreSQL、回测 worker 与各服务进程、巡检本身、磁盘空余、备份年龄与调度。公网、IB Gateway、Xvfb 和告警 webhook 在本实例为跳过。
- 配置周期为 30 秒，首个窗内样本到终点后首个样本之间的最大相邻间隔为 30.749 秒，低于 45 秒缺口阈值。窗内最后一条为 `2026-11-24T05:04:23.238Z`，终点由下一条真实记录 `2026-11-24T05:04:53.221Z` 夹持。可用率按窗内实际成功和失败样本统计。

## 原始记录与复核

原始记录留在隔离测试卷 `/workspace/ha-evidence/qa/ingress-verification/state/box/fertile`。下列 SHA-256 覆盖固定的记录前缀，运行中的日志仍可追加。

| 路径 | 固定记录范围 | SHA-256 |
| --- | --- | --- |
| `logs/healthwatch/samples.jsonl` | 前 2,882 行，含窗前及窗后各一条实测 | `69828469cb2447f3e513adff012352d7c2fb423cb7f82c9ba1cfe53857c0c81c` |
| `logs/alerts/alerts.jsonl` | 前 2 行，含窗前失败及窗口起点恢复 | `0d4f93fc654b34128563cb1a642b8bbc502ad3f827618fb4aa2f0639485a85a8` |

用实际部署中的脚本重新生成报告。退出码为 0，输出与观察器生成的 `/workspace/ha-evidence/delivery-soak-result.md` 逐字节一致。下面的统计表来自该输出。

```sh
soak_root=/workspace/ha-evidence/qa/ingress-verification/state/box/fertile
python3 "$soak_root/current/fertile/scripts/soak_report.py" \
  --samples "$soak_root/logs/healthwatch/samples.jsonl" \
  --alerts "$soak_root/logs/alerts/alerts.jsonl" \
  --start 2026-11-23T05:04:51.397Z --end 2026-11-24T05:04:51.397Z \
  --require-hours 24 --label '历史回测候选 · 隔离 Ubuntu 24.04 · ReplaySource · 公网探针关闭' \
  --output /tmp/fertile-isolated-soak.md
```

## 窗口统计

- 报告窗口：2026-11-23T05:04:51.397000Z 至 2026-11-24T05:04:51.397000Z。
- 窗内样本：2880。首个 2026-11-23T05:04:51.397000Z，末个 2026-11-24T05:04:23.238000Z。
- 配置采样周期（秒）：30。
- 相邻实测样本覆盖 86400 秒，窗口共 86400 秒。缺口 0 段，共 0 秒。
- 连续 24 小时采样门禁：通过。已跳过的探针不计作可用。业务是否成功请查看各探针结果。

## 采样来源

下表来自巡检样本记录的 profile、行情源及发布标识。空值表示未记录。

| 首个样本 | 末个样本 | profile | 行情源 | release | 样本数 |
| --- | --- | --- | --- | --- | ---: |
| 2026-11-23T05:04:51.397000Z | 2026-11-24T05:04:23.238000Z | cloudbox | replay | afc4a5c655eced04c0ba2ff1ef7a90fdef675bac | 2880 |

## 各巡检项可用率

按成功样本占实测样本（成功与失败）计算。缺测表示该轮缺少此项，跳过表示该轮巡检主动标记为 skipped。

| 巡检项 | 成功 | 失败 | 跳过 | 缺测 | 实测可用率 | 跳过原因 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| backup:age | 2880 | 0 | 0 | 0 | 100.00% |  |
| backup:schedule | 2880 | 0 | 0 | 0 | 100.00% |  |
| disk:free | 2880 | 0 | 0 | 0 | 100.00% |  |
| healthwatch:runtime | 2880 | 0 | 0 | 0 | 100.00% |  |
| market:fresh | 2880 | 0 | 0 | 0 | 100.00% |  |
| public:ready | 0 | 0 | 2880 | 0 | 无实测 | public probe disabled for this instance |
| service:finance-api:live | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-api:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-api:ready | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-backtest-worker:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-collector:live | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-collector:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:finance-collector:ready | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:healthwatch:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:ib-gateway:process | 0 | 0 | 2880 | 0 | 无实测 | disabled |
| service:postgres:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:web:live | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:web:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:web:ready | 2880 | 0 | 0 | 0 | 100.00% |  |
| service:xvfb:process | 0 | 0 | 2880 | 0 | 无实测 | disabled |
| supervisor:process | 2880 | 0 | 0 | 0 | 100.00% |  |
| webhook:delivery | 0 | 0 | 2880 | 0 | 无实测 | webhook disabled |

## 行情新鲜度

最大实测行情年龄 33 秒，采样时间 2026-11-24T05:03:53.231000Z。共 2880 轮返回行情年龄。

## 断线与恢复

相邻失败样本归为一次可观测故障段。耗时从首次失败样本算到首次恢复样本。缺测、跳过或采样断档会切断故障段，不能推出缺口内的恢复时刻。

窗口涉及 1 个探针故障段。
各项故障段数：backup:age 1 次。

| 巡检项 | 首次观测失败 | 末次失败或首次恢复 | 结果 | 实测恢复耗时（秒） |
| --- | --- | --- | --- | ---: |
| backup:age | 2026-11-23T05:04:21.429000Z（窗前已失败） | 2026-11-23T05:04:51.397000Z | 已恢复 | 29.968 |

## 采样缺口

间隔超过前一轮设定周期的 1.5 倍时，两端之间整段标为缺测。首个或末个记录以外的时间也计入缺口。

窗口内没有采样断档。

## 实际告警

| UTC | 巡检项 | 事件 | 详情 |
| --- | --- | --- | --- |
| 2026-11-23T05:04:51.397000Z | backup:age | recovery | 12s |

## 备份核验与前序演练

- 窗前 `2026-11-23T05:04:21.429Z` 巡检报告尚无完整备份。启动阶段的快照 `20261123T050438.714866Z` 随后落盘并通过 `scripts/restore.sh "$FERTILE_ROOT/backups/20261123T050438.714866Z" --dry-run` 校验，在窗口起点 `2026-11-23T05:04:51.397Z` 观测到恢复。两个观测点相隔 29.968 秒。
- 窗内的 `logs/healthwatch/backup.log` 记录了 `20261124T030023.938563Z` 快照，源为 replay，数据库为 web。对启动快照与该窗口内快照分别执行 `sha256sum --strict --check --quiet SHA256SUMS`，两次均退出 0，分别验证 18 和 1,334 项。清单包含 PostgreSQL 归档、作业 SQLite 和行情、研究目录。
- 更早的 `e9f25ad` 隔离候选另做了真实 PostgreSQL 17.12、SQLite、Parquet 备份恢复，以及协议夹具断网回补。其过程和较早隔离阶段记录的告警保留在原交付记录。本报告的窗口统计全部来自上文标记的 `afc4a5c` ReplaySource 实例。[历史回测与恢复演练](2026-11-23-historical-backtest.md) · [PR 中的早期磁盘告警](https://github.com/viramoon/fertile/pull/12)

## 剩余验收

跨仓 GitHub Actions 集成仍待仓库维护者提供三子仓最小读取权限，再由实现者重跑门禁。真实 IBKR paper、交易时段行情、公网、目标盒子恢复和生产 24 小时由 Ruler 在已合并的主分支集成版本上另行验收。Issue #9 保持开放。[权限预检](https://github.com/viramoon/fertile/actions/runs/45115149038/job/137496188455) · [Ruler 部署手册](../ruler-runbook.md) · [#9 验收要求](https://github.com/viramoon/fertile/issues/9#issuecomment-7095401596)
