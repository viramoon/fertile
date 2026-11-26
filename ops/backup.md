# 盒子备份与恢复

巡检默认每天 UTC 03:00 调用一次 `scripts/backup.sh`。时间和备份过期阈值由 [巡检配置](../deploy/healthwatch.yaml) 决定。手动执行相同命令会输出完成的目录名。每个快照保存在 `$FERTILE_ROOT/backups/<UTC 时间戳>/`，只有在文件落盘、生成 SHA256 清单后才以该名称发布。并发备份与正式恢复由 `run/backup.lock` 串行化。

## 数据与位置

- PostgreSQL 17 使用 `pg_dump --format=custom --create` 导出整个 web 库，包含表、数据、自增序列和数据库级权限。集群角色、服务器扩展或自定义表空间须在目标 PG 实例先准备好。
- 从 `$FERTILE_DATA_ROOT/finance-state/` 保存业务状态。SQLite 数据库使用在线备份接口和 `quick_check`，包含 WAL 中已提交的记录。进行中的作业临时目录、锁文件与 SQLite sidecar 会被排除。
- 先保存原子发布的 collector 水位，再枚举 catalog 的正式 Parquet 与来源标记。若采集器同时发布新文件，快照可以包含比记录水位更新的数据。collector 恢复时从磁盘重建水位并去重回放。备份和恢复会检查 `collector/catalog.json` 与 `.finance-collector/source.json` 一致。首次备份复制 Parquet；之后同路径内容与上一快照 SHA256 相同则硬链接至前一次快照。恢复的活动文件是独立副本。
- 保存持久目录 `$FERTILE_ROOT/config/` 中的非机密配置。部署代码、锁定依赖和默认配置由 release/gitlink 提供。密钥须放在 `$FERTILE_ROOT/secrets/` 或对应的密钥目录。这些目录、Cloudflare 凭据、Gateway 登录状态均在备份范围之外。配置中的私有密钥文件名和额外的 `.env` 被排除，唯一公开环境配置是 `config/runtime.env`。该文件及普通 YAML 不应包含密钥。
- PG 的逻辑快照与 finance 的快照各自一致，对账时应保留原作业 ID 和幂等键。快照没有跨两个存储的共同事务时点。仅保留本机备份时，整机或整块持久盘丢失就无法恢复。

## 私人 Google Drive 加密备份

异地备份默认关闭。启用后，日全量默认于 UTC 03:00 创建，每份全量可独立恢复。小时增量以已验证的恢复点为父点，记录增删与变更。PG 与 SQLite 继续使用本机快照的逻辑导出与在线备份方式。每个存储的快照截点独立，恢复后应按作业、策略版本、事件位置与幂等键对账。

上传和读取校验均经 `rclone crypt`。加密覆盖内容、文件名、目录名和清单。工具核对文件集合、长度和 SHA256，在数据对象通过校验后发布恢复点清单。上传失败不会发布新恢复点。保留策略默认选择最近 7 个 UTC 日点、4 个 ISO 周点及这些恢复点所依赖的备份。外部生命周期规则请勿直接作用于该专用备份目录。

### 首次启用

由 vira 选择私人 Drive 账户，确认共享配额中可供备份使用的容量，并选择离线恢复密钥保管方式。容量估算应包含每日完整集、小时增量、短期失败重试和保留窗口。按实际快照大小和带宽测量首次全量所需时间。仅将获准用于私人加密备份的数据纳入异地备份。启用前核对传统采集 catalog、研究 catalog 和持久业务状态的配置路径及对应许可。

真实 OAuth 和 `crypt` 密码及独立盐只在部署机的 `$FERTILE_ROOT/secrets/backup-rclone.conf` 中配置。秘密目录由运行账户持有，权限为 `0700`，配置文件为同一账户持有的 `0600` 普通文件。非机密备份选项放 `$FERTILE_ROOT/config/backup.conf`。在本机 rclone 配置中创建命名的私人 `drive` 后端，将 `root_folder_id` 指向专用备份目录，再创建包装该后端的 `crypt` 目标。该根只存放同一套 `crypt` 配置写入的内容，避免其他文件或其他密钥写入的目录导致解密目录名时失败。备份目录下使用 `filename_encryption = standard`、`directory_name_encryption = true` 和默认的内容加密，配置 `password` 和独立的 `password2`。配置文件内 rclone 混淆后的密码和盐仍按明文密钥保管。

把离线副本与部署盘分开保管。恢复材料包括 `crypt` 密码、盐、文件名加密参数、Drive 账户及专用目录的定位信息，以及在新主机重新取得该 Drive 读权限的方法。在隔离空目录验证可用后记录演练日期与结果。更换密码或盐时，为新配置分配独立的 Drive 目录和命名目标，保有旧密钥及其对应恢复点，直至在新目标上验证独立全量。更换 OAuth 或底层目录后，先执行实时 `status` 刷新本机观测再恢复调度。缓存按命名目标区分，沿用相同目标名时，底层目录变更前应暂停发布，并移除旧的 `run/backup-remote-status.json` 及 `run/backup-remote-restore.json`。切换完成后重新验证恢复。

### 调度、状态与轮转

首次启用后先手工运行一次 `full`，建立小时增量的起点。一个远端归一个发布实例，使用同一 `FERTILE_ROOT` 协调发布和轮转。supervisord 实例通过 healthwatch 调度。日全量默认在 UTC 03:00，小时增量在整点到期。到期任务可补跑。失败后按 15 分钟间隔重试。全量与小时任务可分别上传，各自的恢复点按截点发布。普通服务器按需为两个命令编排独立的 systemd timer，分别使用 `OnCalendar=*-*-* 03:00:00 UTC` 和 `OnCalendar=*-*-* *:00:00 UTC`，设 `Persistent=true`。对应的 oneshot 服务以实例运行账户执行，显式指定同一 `FERTILE_ROOT` 和发布目录，并为任务提供 PG 客户端路径。每台实例使用一种调度方式。

```bash
python3 scripts/backup_remote.py full
python3 scripts/backup_remote.py hourly
python3 scripts/backup_remote.py status
python3 scripts/backup_remote.py status --cached
```

`status` 实时读取远端候选恢复链的清单和数据对象，解密后核对长度和 SHA256。候选链损坏时报告更早的已验证恢复点，若无完整链则返回空水位。每次小时备份也会核验选用的父链，再上传本次新增或变更的对象。状态和父链校验以流式读取计算对象摘要。估算小时窗口时应计入完整候选链的读取带宽，上传读回和正式恢复另需暂存空间。

`status --cached` 和 healthwatch 从 `run/backup-remote-status.json` 读取最近观测，采样无需远端请求。小时任务确认已有点从远端消失或无法通过完整性检查后会更新缓存，即使该次任务未能发布新点。没有有效全量时，缓存中的全量时间为空，日全量按调度规则补跑。远端访问故障导致实时检查无法确认状态时，保留最近已知观测并标记降级，供排障使用。

`last_recoverable_at` 为最后可恢复到的 UTC 时间，`last_recoverable_point` 为可传给恢复命令的点 ID。`age_seconds` 是距该时间的秒数。没有合格点、时间在未来、点龄超过 3600 秒或出现降级时，`alert` 为真。`degraded` 表示远端观测或恢复链出现故障。`last_full_at`、`last_hourly_at` 分别是可用于调度判断的日全量与小时点时间。时间为 UTC，时间和年龄缺值时为 `null`。恢复点时间用于衡量整盘丢失时的恢复进度，行情源自身的缺口和恢复耗时分别记录。

日点和周点均从独立全量中选择。保留最近 7 个有全量的 UTC 日期及其小时链、最近 4 个有全量的 ISO 周各自最新的全量、最新可恢复点的父链和演练凭据所引用的父链。执行 `python3 scripts/backup_remote.py prune` 显式轮转。日全量在满足轮转条件时也尝试轮转。两种方式均要求最近 7 天内成功完成一次实际隔离恢复。每次轮转先核对远端清单、父链与对象清册，再流式解密并校验所有保留点及其依赖对象的当前 SHA256。所有校验通过后才开始删除。估算轮转窗口时应计入读取这些数据的带宽和独占锁持有时间。Drive 默认删除到回收站，空间不足时先检查共享配额、回收站和备份规模。将容量调整交给运维。

上传故障后重试会使用新恢复点。无提交清单的对象不会成为恢复点。通过轮转检查后，工具清理超过七天的此类对象。检查发现远端清册异常、保留链对象损坏或读取故障时，本次轮转在删除前终止，保留旧恢复点和未提交对象供排查。本机通常会在命令退出时清理 `$FERTILE_ROOT/backups/.remote-*` 暂存，强制终止后可能留下残余。确认无备份或恢复进程使用对应暂存目录，再按现场空间需求处理。

### 远端文件与 F3 接口

解密视图中，`commits/<点 ID>.json` 是已提交的恢复点清单。点 ID 为开始采集的 UTC 时间，格式 `YYYYMMDDTHHMMSS.ffffffZ`。清单 v1 的 `format` 为整数 `1`，`id` 为点 ID，`kind` 为 `full` 或 `hourly`。`parent` 是 `null` 或包含父点 `id` 和清单 `sha256` 的对象。`snapshot` 包含本机 `snapshot.json` 的元数据，`snapshot.captured_at` 以相同的时间格式记录已采集的 `postgres`、`finance-state`、`catalog`、`config`、`research` 各自完成时刻。`files` 按相对路径映射为 `{ "sha256": "…", "bytes": 整数 }`，`deleted` 是移除的相对路径数组。`objects/<点 ID>/<SHA256>` 保存本点新写入的不可变对象。小时点继承父点的未变对象，全量的对象都可在本点取得。恢复端按父链核对对象名称、大小、摘要和重建的本机快照。远端清单与本机状态 JSON 为 CLI/文件接口。 [命令索引](../scripts/README.md)

F3 接口使用 `finance-market-wal --data-root <FERTILE_DATA_ROOT>` 的 `products` 与 `manifest`，并消费封段清单 v1 和清单枚举的文件。该接口必须返回获准用于私人 Drive 备份的已封段前缀。活动段、内部 checkpoint、锁和可重建的 market projection 不进入异地包。配置了 F3 的实例需提供 `FERTILE_BACKUP_WAL_BIN`。默认检查 CLI 枚举的全部产品，其中任一产品缺少许可即停止发布。许可混合时，用 `FERTILE_BACKUP_WAL_PRODUCTS` 以逗号分隔列出获准备份的准确 `source/product` 对。所选产品不存在或缺少 `ProductPolicy` 许可时停止发布。

生产者活跃时先由其 `seal_for_backup()` 发布封段。备份 CLI 此时读取可见的已封段前缀。写者已停时，可向 `full` 或 `hourly` 传 `--seal-wal` 让 CLI 封段。快照内 `wal/manifests/<source>/<product>.json` 保留 F3 清单，`wal/segments/` 内依清单相对路径保存段文件。`snapshot.wal` 按源、产品记录十进制字符串 `sealed_through`。正式恢复核对这些截点，通过 F3 的 `restore --manifest PATH` 在暂存根激活 WAL。核对业务前在隔离根重放投影。 [F3 WAL 接口](https://github.com/viramoon/finance/pull/47)

### 从远端恢复到指定水位

在独立的运行根准备工具、PG 实例和密钥配置，为业务恢复选择快照记录的行情源。密钥丢失时从离线保管处取回命名加密配置所需信息。用 `status` 查最新可恢复点的 ID，或从既有运行记录查历史点 ID。显式指定点 ID 时只读取该点及其父链，独立日全量可在其他清单损坏时恢复。使用 `latest` 时，工具由新到旧验证候选链，较新的清单或对象损坏时回退到仍可验证的点。验证通过后才写入恢复目标。以下第一步只重建普通快照，路径必须为尚不存在的具体绝对目录。

```bash
python3 scripts/backup_remote.py restore "$point" --snapshot-to "$snapshot_dir"
scripts/restore.sh "$snapshot_dir" --dry-run
python3 scripts/backup_remote.py restore "$point" \
  --target-data-root "$empty_data_root" --target-database "$empty_db"
```

正式恢复选择已停止写入的独立空数据根和目标库。成功后在 `run/backup-remote-restore.json` 写入用于轮转的演练凭据。已安装 release 的快照通过 `snapshot.release` 记录 fertile 与三个子仓提交，可据此选择对应的 F2 策略注册表版本。核对 PG 业务表、outbox 去重键、策略版本、SQLite 作业与幂等键、Parquet 清单、各产品封段截点和已知行情缺口。记录恢复点时刻、独立恢复耗时和业务核对结果。链中断时检查密钥与对象完整性，选择较早的完整链或一个独立全量。保留损坏现场供追查。

### 隔离演练

演练使 rclone `crypt` 包住一次性 `/tmp` 的 `local` 后端，现场生成测试密钥和盐。用例覆盖加密链恢复、上传失败、断链、缺失密钥和容量不足，以及无关清单损坏、等长对象篡改与全量丢失后的水位。检查底层 local 目录中的名称和原始字节，确认备份文件名、清单和合成数据均已加密。使用合成或回放数据，沿用 IBKR paper 只读边界。

```bash
export FERTILE_TEST_RCLONE=/absolute/path/to/rclone
python3 -B tests/backup_crypt_test.py
python3 -B tests/backup_remote_test.py
python3 -B tests/backup_wal.py
python3 -B tests/backup_schedule.py
```

将 PostgreSQL 17 的服务端和客户端工具加入 `PATH` 并设 `FERTILE_TEST_POSTGRES=1`，可在 `backup_remote_test.py` 中一并演练独立 Unix socket 上的空库恢复。`FERTILE_BACKUP_TEST_WAL` 可选择指定本地 F3 CLI 的绝对路径，用合成封段验证真实 CLI 的恢复和再读取。`backup_schedule.py` 使用控制端的 PyYAML，模拟时钟和子进程检查日、小时调度及告警。

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
| `FERTILE_BACKUP_REMOTE` | 空 | 启用异地备份时填已命名的 `crypt` 目标，例如 `archive:fertile`。 |
| `FERTILE_BACKUP_RCLONE` | `/usr/bin/rclone` | rclone v1.76.0 可执行文件的绝对路径。 |
| `FERTILE_BACKUP_WAL_BIN` | 空 | F3 实例填写 `finance-market-wal` 可执行文件的绝对路径。检测到 F3 数据时需要该配置。 |
| `FERTILE_BACKUP_WAL_PRODUCTS` | 未设置 | 可选，逗号分隔的 `source/product` 精确列表。省略时要求 F3 枚举的所有产品均获准备份。 |

本机保留集合取日备份与周备份的并集。按周或按日重复命中的同一快照只占一份。本机 `scripts/backup.sh` 发布和轮转本机快照，远端发布由 `scripts/backup_remote.py` 执行。旧配置若曾设 `FERTILE_BACKUP_REMOTE`，启用前先迁移到已命名的 `crypt` 目标和专用本机配置文件。旧远端快照可通过原有归档途径恢复，新链由新日全量起步。

部署前可在 `config/backup.conf` 保留以下占位，待账户、权限和离线保管条件确认后再填写 `crypt` 目标。

```dotenv
FERTILE_BACKUP_REMOTE=
FERTILE_BACKUP_RCLONE=/usr/bin/rclone
# 启用时将 FERTILE_BACKUP_REMOTE 填为命名目标，例如 archive:fertile。
# 接入 F3 时再设置 FERTILE_BACKUP_WAL_BIN 的绝对路径。
# 混合许可实例为 FERTILE_BACKUP_WAL_PRODUCTS 填写获准的 source/product。
```

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
