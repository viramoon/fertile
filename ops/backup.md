# 盒子备份与恢复

巡检默认每天 UTC 03:00 调用一次 `scripts/backup.sh`。时间和备份过期阈值由 [巡检配置](../deploy/healthwatch.yaml) 决定。手动执行相同命令会输出完成的目录名。每个快照保存在 `$FERTILE_ROOT/backups/<UTC 时间戳>/`，只有在文件落盘、生成 SHA256 清单后才以该名称发布。并发备份与正式恢复由 `run/backup.lock` 串行化。

## 数据与位置

- PostgreSQL 17 使用 `pg_dump --format=custom --create` 导出整个 web 库，包含表、数据、自增序列和数据库级权限。集群角色、服务器扩展或自定义表空间须在目标 PG 实例先准备好。
- 从 `$FERTILE_DATA_ROOT/finance-state/` 保存业务状态。SQLite 数据库使用在线备份接口和 `quick_check`，包含 WAL 中已提交的记录。进行中的作业临时目录、锁文件与 SQLite sidecar 会被排除。
- 先保存原子发布的 collector 水位，再枚举 catalog 的正式 Parquet 与来源标记。若采集器同时发布新文件，快照可以包含比记录水位更新的数据。collector 恢复时从磁盘重建水位并去重回放。备份和恢复会检查 `collector/catalog.json` 与 `.finance-collector/source.json` 一致。首次备份复制 Parquet；之后同路径内容与上一快照 SHA256 相同则硬链接至前一次快照。恢复的活动文件是独立副本。
- 保存持久目录 `$FERTILE_ROOT/config/` 中的非机密配置。部署代码、锁定依赖和默认配置由 release/gitlink 提供。密钥须放在 `$FERTILE_ROOT/secrets/` 或对应的密钥目录。这些目录、Cloudflare 凭据、Gateway 登录状态均在备份范围之外。配置中的私有密钥文件名和额外的 `.env` 被排除，唯一公开环境配置是 `config/runtime.env`。该文件及普通 YAML 不应包含密钥。
- PG 的逻辑快照与 finance 的快照各自一致，对账时应保留原作业 ID 和幂等键。快照没有跨两个存储的共同事务时点。仅保留本机备份时，整机或整块持久盘丢失就无法恢复。

## 非活动旧 SPY catalog 的单独备份

日常 `scripts/backup.sh` 的行情与队列备份以当前活动 `FERTILE_DATA_ROOT` 为准。本机活动根是 `data-paper`，因此日常快照不包含非活动 catalog 下 `$FERTILE_ROOT/data/catalog/data/bar/SPY.XNAS-1-DAY-LAST-EXTERNAL` 的固定 21:00 UTC 日线及同一 catalog 下的 `data/equity/SPY.XNAS`。

迁移前确认旧 catalog 已无写入方，以下命令将日线和 equity 元数据复制到日常备份轮转之外的新目录，保留 Nautilus catalog 目录结构。可将 `legacy_backup_parent` 改为另一个独立的持久绝对路径。两次复制都成功后才会打印备份 catalog 路径。复制失败时保留原件，排查原因后用新目录重新复制。

```bash
legacy_catalog="${FERTILE_ROOT:?先设置实例的持久根}/data/catalog"
legacy_backup_parent="$HOME/fertile-legacy-spy-backups"
legacy_backup=$(
  mkdir -p -- "$legacy_backup_parent" &&
  copy_dir=$(mktemp -d "$legacy_backup_parent/spy-21utc.XXXXXXXX") &&
  mkdir -p -- "$copy_dir/catalog/data/bar" "$copy_dir/catalog/data/equity" &&
  cp -a -- "$legacy_catalog/data/bar/SPY.XNAS-1-DAY-LAST-EXTERNAL" "$copy_dir/catalog/data/bar/" &&
  cp -a -- "$legacy_catalog/data/equity/SPY.XNAS" "$copy_dir/catalog/data/equity/" &&
  printf '%s\n' "$copy_dir"
) && printf '%s\n' "$legacy_backup/catalog"
```

在 `$legacy_backup` 下记录备份时间、源目录和文件、提供方及原消费方、标的 `SPY.XNAS`、实际首尾日期和条数、固定 21:00 UTC 时间口径、OHLCV 和复权口径。后续迁移将这份副本作为 `--source-catalog`，原件保持只读。

## 配置

手工操作先加载本实例的 `env.sh`，使用与服务相同的 `config/runtime.env`。备份要求活动数据根已挂载，路径遵循 `FERTILE_DATA_ROOT` 及 `FERTILE_MARKET_SOURCE`。IB 默认为 `data/`，回放默认为 `data/replay/`。PG 默认连接 `$FERTILE_ROOT/run/postgres`，端口来自 `FERTILE_POSTGRES_PORT`。备份库名依序取 `PGDATABASE`、`FERTILE_WEB_DATABASE`、`web`。恢复库名由 `--target-database` 指定。若 web 使用外部 PG，备份和恢复需提供对应的 libpq `PGHOST`、`PGPORT` 与角色认证，备份另传 `PGDATABASE`。

可在 `$FERTILE_ROOT/config/backup.conf` 设置以下非机密值。进程环境中的同名变量优先。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `FERTILE_BACKUP_DAYS` | 7 | 最近几个有备份的 UTC 日期各保留当日最新一份，最小 1 |
| `FERTILE_BACKUP_WEEKS` | 4 | 最近几个有备份的 ISO 周各保留当周最新一份，可设 0 |
| `FERTILE_BACKUP_CATALOG` | `$FERTILE_DATA_ROOT/catalog` | 自定义 collector catalog 时指向实际已挂载的绝对路径 |
| `FERTILE_BACKUP_REMOTE` | 空 | 可选的已命名 rclone remote，例如 `archive:fertile`；启用时先在盒子外配置好 rclone 的认证与加密 |

保留集合取日备份与周备份的并集。按周或按日重复命中的同一快照只占一份。清理仅针对本机已有完整快照；对象存储保留策略由其生命周期规则负责。启用远端复制时，命令在本机发布后执行 `rclone copy`。远端失败会返回非零，本机已完成的快照仍可恢复。

## 恢复步骤

以运行服务的非 root 账户操作。使用有建库权限、能读取和改名已有目标库、能还原对象所有者及授权的 PG 管理角色。本机默认 PostgreSQL 管理员通过 Unix socket peer 连接。目标实例应先具备归档引用的所有角色（本机包括 `fertile_web`）、扩展和表空间。预留一份目标现状备份、一套暂存状态文件和一套数据库所需空间。使用受信任的备份，SHA256 用于发现传输和存储损坏。

```bash
source "${FERTILE_ROOT:?先设置实例的持久根}/env.sh"
snapshot="$FERTILE_ROOT/backups/<已选快照时间戳>"
target_data="$FERTILE_ROOT/data-paper-rollback"
target_db=web_rollback
scripts/restore.sh "$snapshot" --dry-run
(cd "$snapshot" && sha256sum --check SHA256SUMS)
```

`--dry-run` 核对所有文件的 SHA256、快照结构、行情来源和 PG 归档格式。仅传快照时可在 PostgreSQL 停止期间运行。同时指定 `--target-data-root` 和 `--target-database` 后，dry-run 还会核对路径和库名是否合法，并检查数据根、各目标目录及目标库是否被占用。检查需要连到 PostgreSQL 的 `postgres` 维护库。目标库不存在时视为空，已存在时须能连接该库并读取其应用 schema、对象定义和大对象元数据。数据库不可达或查询失败时，命令会在标准错误中点名目标库并退出 1。

发现占用且未加 `--overwrite` 时，命令在标准错误中输出 `non-empty restore directory` 或 `non-empty target database`，点名目标并退出 1。加 `--overwrite` 后，占用目标会收到 `Warning:`，说明正式恢复会替换、移除或保留哪些内容。其余检查都通过时退出 0。以退出码判断整次检查是否通过，因为 `SHA256 verified` 可能先于占用错误输出。dry-run 只读检查，不创建目录、数据库、锁、恢复日志或临时归档。正式恢复仍需停止写入方，并在未加 `--overwrite` 时重新检查占用。

下例使用 supervisord 停止业务。systemd 实例先由管理员执行 `sudo systemctl stop fertile.target`。

```bash
scripts/fertile down
scripts/postgres.sh start
scripts/restore.sh "$snapshot" --dry-run --target-data-root "$target_data" --target-database "$target_db"
scripts/restore.sh "$snapshot" --target-data-root "$target_data" --target-database "$target_db"
scripts/postgres.sh stop
```

手工单独启动 PG 时，把 `FERTILE_POSTGRES_PORT` 设为现场值。supervisord 实例的发布与运行指针切换见 [Ruler 回滚手册](ruler-runbook.md#从升级前快照回滚并切回新版)。systemd 实例按 [普通服务器部署指南](../deploy/linux-server.md#systemd) 切换发布和启动整栈。

- 写入模式同时要求 `--target-data-root` 和 `--target-database`。数据根为具体的绝对目录，父目录须存在。默认将业务状态、行情 catalog 和研究目录恢复到目标的 `finance-state/`、`catalog/`、`research/`。可通过 `--target-research-root` 显式选择 `$FERTILE_ROOT/data/research` 下已有父目录的路径，用于恢复独立配置的研究目录。快照内的 `config/` 仅供核对。
- 非空数据根、非空研究目标或包含应用对象的目标库需要 `--overwrite`。目标库检查涵盖应用 schema、对象和大对象。覆盖仅替换快照管理的目录和有归档的目标库，数据根其余文件保留。快照缺少数据库归档时，目标库原样保留。快照尚无 `research` 项时，所选研究目录保留。
- 库名一致时使用 `pg_restore --create`，保留归档中的数据库级元数据。库名不同时，先用 `template0` 创建指定库，再用 `pg_restore --dbname <目标库>` 导入 custom 归档。省略 `--create` 后，原库名仅用于核对归档与清单，PG 继续恢复对象所有者和对象级 ACL。数据库级所有者、ACL、注释、locale、表空间和 `ALTER ROLE ... IN DATABASE` 等设置采用新库的默认值或由 DBA 单独配置。对于受管 web 库，控制器启动时会为 `fertile_web` 补上连接权限和 SQLx 搜索路径。
- 写入前取得 `run/control.lock`，为目标现状生成一份 `Before-restore snapshot`。旧目标库在恢复期间改为临时名称。`run/restore-journal.json` 保存进度。断电或 `SIGKILL` 后，在同一 PG 实例恢复可用、业务保持停止的情况下，带同一组显式目标重试同一条命令。工具先回滚未提交的切换或完成已提交的清理，再校验快照。等待操作成功再启动业务。

## 验证

CI 用私有 Unix socket 和一次性 PostgreSQL 17 数据目录运行以下演练。测试依赖 Python 3.11+、PyArrow 26.0.1 和 PostgreSQL 服务端工具。备份与恢复脚本本身只依赖 Python 标准库和 PG 客户端。

```bash
export PATH="$(pg_config --bindir):$PATH"
python3 tests/backup_restore.py
```

演练包含精确 PG 金额、中文记录、序列、ACL、SQLite WAL、定点 Parquet、硬链接、秘密排除、删库恢复和 SIGKILL 中断重试。独立目标演练检查 dry-run 对空库和非空库的处理、空库恢复、改名恢复、原库与原目录的隔离，以及覆盖失败后的回滚。缺 PG 服务端工具时，离线校验、目录占用和轮转用例照常执行。只有真实 Parquet 演练依赖 PyArrow。

研究日线使用运行配置 FERTILE_RESEARCH_ROOT 指向的持久 catalog。快照包含已发布的 Parquet 和每标的 dataset.json，排除导入暂存目录。验证恢复时同时核对来源 SHA、区间、作业队列与回测摘要。恢复早于研究功能的旧快照会保留当前研究目录。需要切换数据批次时先停止接单并让作业结束，再由 Ruler 执行快照和配置切换。
