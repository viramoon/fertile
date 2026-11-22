# 盒子备份与恢复

巡检默认每天 UTC 03:00 调用一次 `scripts/backup.sh`。时间和备份过期阈值由 [巡检配置](../deploy/healthwatch.yaml) 决定。手动执行相同命令会输出完成的目录名。每个快照保存在 `$FERTILE_ROOT/backups/<UTC 时间戳>/`，只有在文件落盘、生成 SHA256 清单后才以该名称发布。并发备份与恢复由 `run/backup.lock` 串行化。

## 数据与位置

- PostgreSQL 17 使用 `pg_dump --format=custom --create` 导出整个 web 库，包含表、数据、自增序列和数据库级权限。集群角色、服务器扩展或自定义表空间须在目标 PG 实例先准备好。
- 从 `$FERTILE_DATA_ROOT/finance-state/` 保存业务状态。SQLite 数据库使用在线备份接口和 `quick_check`，包含 WAL 中已提交的记录。进行中的作业临时目录、锁文件与 SQLite sidecar 会被排除。
- 先保存原子发布的 collector 水位，再枚举 catalog 的正式 Parquet 与来源标记。若采集器同时发布新文件，快照可以包含比记录水位更新的数据。collector 恢复时从磁盘重建水位并去重回放。备份和恢复会检查 `collector/catalog.json` 与 `.finance-collector/source.json` 一致。首次备份复制 Parquet；之后同路径内容与上一快照 SHA256 相同则硬链接至前一次快照。恢复的活动文件是独立副本。
- 保存持久目录 `$FERTILE_ROOT/config/` 中的非机密配置。部署代码、锁定依赖和默认配置由 release/gitlink 提供。密钥须放在 `$FERTILE_ROOT/secrets/` 或对应的密钥目录。这些目录、Cloudflare 凭据、Gateway 登录状态均在备份范围之外。配置中的私有密钥文件名和额外的 `.env` 被排除，唯一公开环境配置是 `config/runtime.env`。该文件及普通 YAML 不应包含密钥。
- PG 的逻辑快照与 finance 的快照各自一致，对账时应保留原作业 ID 和幂等键。快照没有跨两个存储的共同事务时点。仅保留本机备份时，整机或整块持久盘丢失就无法恢复。

## 配置

默认持久根为 `/home/box/fertile`。手工操作先加载引导环境，并使用与 supervisor 相同的 `config/runtime.env`。数据根遵循 `FERTILE_DATA_ROOT` 及 `FERTILE_MARKET_SOURCE`，IB 默认为 `data/`，回放默认为 `data/replay/`。工具要求活动数据根已挂载。PG 默认连接 `$FERTILE_ROOT/run/postgres` 上的 `web` 库，端口来自 `FERTILE_POSTGRES_PORT`。如 web 单独配置了连接目标，执行备份、恢复时均应提供对应的 libpq `PGHOST`、`PGPORT`、`PGDATABASE` 和角色凭据；操作命令使用库名，不打印连接密钥。

可在 `$FERTILE_ROOT/config/backup.conf` 设置以下非机密值。进程环境中的同名变量优先。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `FERTILE_BACKUP_DAYS` | 7 | 最近几个有备份的 UTC 日期各保留当日最新一份，最小 1 |
| `FERTILE_BACKUP_WEEKS` | 4 | 最近几个有备份的 ISO 周各保留当周最新一份，可设 0 |
| `FERTILE_BACKUP_CATALOG` | `$FERTILE_DATA_ROOT/catalog` | 自定义 collector catalog 时指向实际已挂载的绝对路径 |
| `FERTILE_BACKUP_REMOTE` | 空 | 可选的已命名 rclone remote，例如 `archive:fertile`；启用时先在盒子外配置好 rclone 的认证与加密 |

保留集合取日备份与周备份的并集。按周或按日重复命中的同一快照只占一份。清理仅针对本机已有完整快照；对象存储保留策略由其生命周期规则负责。启用远端复制时，命令在本机发布后执行 `rclone copy`。远端失败会返回非零，本机已完成的快照仍可恢复。

## 恢复步骤

以运行服务的非 root 账户操作。PG 恢复角色需能建库、改库名，并能还原备份中的对象所有者及权限。pg_dump 按数据库保存对象和 ACL；在全新的 PG 集群先运行同批 scripts/fertile up，创建本机受限的 fertile_web 角色，再按下方流程停止业务、恢复快照。外部管理的角色须由数据库管理员先行建立。预留一份现状备份、一套暂存状态文件和一套数据库的额外空间。只选择受信任的备份；SHA256 用于发现传输和存储损坏。

```bash
source /home/box/fertile/env.sh
snapshot=$(scripts/backup.sh)
scripts/restore.sh --dry-run "$snapshot"
(cd "$snapshot" && sha256sum --check SHA256SUMS)
```

`--dry-run` 校验全部文件、目录边界、来源与 PG 归档格式。恢复使用生成上述快照时的源类型、数据库名和目标数据挂载，PG 可从空数据库实例恢复。恢复前停掉 supervisor 中的业务服务，只启动 PostgreSQL。

```bash
scripts/fertile down
scripts/postgres.sh start
scripts/restore.sh "$snapshot"
scripts/postgres.sh stop
scripts/fertile ensure
scripts/fertile status --json
```

恢复会额外取得 `run/control.lock`，先给现状再备一份并打印路径。若目标数据库已丢失，该快照记录数据库缺失并保存尚在的本地状态。对于这种仅有本地状态的快照，恢复会保留当前 PG 库。工具把现有目标库暂时改名，将 PG 归档按原名整库还原，再切换状态、catalog 和配置。切换期间的 `run/restore-journal.json` 保留事务进度。断电或 `SIGKILL` 后加载相同环境、只启动 PG，再次执行同一条 restore 命令；它会回滚未提交的切换或清理已完成的切换，然后重新核验。保留日志并等待 restore 返回成功再执行 ensure。

## 验证

CI 用私有 Unix socket 和一次性 PostgreSQL 17 数据目录运行以下演练。测试依赖 Python 3.11+、PyArrow 26.0.1 和 PostgreSQL 服务端工具。备份与恢复脚本本身只依赖 Python 标准库和 PG 客户端。

```bash
export PATH="$(pg_config --bindir):$PATH"
python3 tests/backup_restore.py
```

演练包括两个有 15 位小数的 PG 金额、中文记录、序列及 ACL；持有未提交写事务的 SQLite WAL；真实定点 Parquet；硬链接、秘密排除、清单篡改、删库恢复、已有库替换及 SIGKILL 中断重试。另用 42 天共 84 份快照检查 7 日与 4 周轮转。
