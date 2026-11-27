# 配置与密钥

应用按 config/base.yaml、config/<profile>.yaml、显式环境变量顺序加载，后者覆盖前者。部署 profile 为 cloudbox 或 stable。Nautilus 的回测 / 沙盒 / Live 是另一个维度；当前 Gateway 仅接受 paper 账户。frontend 只访问同源 web API。

## 持久运行配置

在运行根 `config/runtime.env` 中配置每行一个非敏感 `KEY=VALUE`。控制器按允许的变量列表加载。supervisord 模式下，显式传给控制命令的同名变量优先。systemd 服务各自从文件加载运行参数。长期配置保存在文件中，启动、恢复、巡检使用相同的设置。

| 参数 | 生效位置 |
| --- | --- |
| FERTILE_ROOT、FERTILE_BOX_HOME | 先取已导出的 `FERTILE_ROOT`，其次取 `$FERTILE_BOX_HOME/fertile`。两者均未设置时，若已导出 `FERTILE_PROFILE=stable` 则取 `/var/lib/fertile`，否则取 `/home/box/fertile`。bootstrap 生成的 `env.sh` 保存实际路径。选择持久根发生在读取 `runtime.env` 之前。 |
| FERTILE_MARKET_SOURCE | 默认 ib。测试使用 replay，独立存入 data/replay。生产源与回放源切换需核对 catalog 与队列目录。 |
| FERTILE_PROFILE、FERTILE_DATA_ROOT | 选择服务配置和持久数据目录。默认 profile 为 `cloudbox`。普通服务器在运行文件中设置 `FERTILE_PROFILE=stable`。显式数据挂载必须存在。 |
| FINANCE_API_PORT、FINANCE_COLLECTOR_PORT、WEB_PORT | 默认在 `127.0.0.1` 监听 8081、8082、8090。8080 保留给现有网站。 |
| FERTILE_POSTGRES_PORT | 受管 PostgreSQL 的 Unix socket 端口编号，默认 5432。 |
| FERTILE_RESEARCH_ROOT | 默认为当前 FERTILE_ROOT 下的 data/research，API、worker、备份共用。可显式设置为该目录本身或其具体子目录，恢复到独立数据根时也可设为 `$FERTILE_DATA_ROOT/research`。使用具体的绝对路径。 |
| FERTILE_WEB_DATABASE | 受管本机 web 的数据库名称，默认 `web`。可通过控制器命令环境临时选择已恢复的库。使用普通 PostgreSQL 库名，开头为字母或下划线，随后可含字母、数字或下划线，最多 63 字符。 |
| FINANCE_COLLECTOR_CONFIG、IB_GATEWAY_ADDRESS | 指向当前行情源的配置和本机 paper 接口 127.0.0.1:4002。 |
| FERTILE_ENABLE_FINANCE_PROJECTOR、FERTILE_ENABLE_WEB_NOTIFIER | 值为 `1` 时启用对应进程。projector 需要 F3 模块和 WAL 命令，notifier 需要 W3 制品、已迁移的数据库及专用环境文件。默认关闭。[进程条件与交接](../deploy/hot-upgrade.md#宿主与持久目录) |
| FERTILE_HEALTH_CONFIG | 站点巡检覆盖文件的路径。可在未来生产服务器的文件里显式填写 `public_origin`。 |
| FERTILE_PUBLIC_ORIGIN | 运维显式启用公网巡检的 HTTPS origin，如 `https://<your-origin>`。非空环境变量优先于覆盖文件。 |
| FERTILE_BACKUP_DAYS、FERTILE_BACKUP_WEEKS | 本机与异地备份的日、周保留点数。可写入 `config/backup.conf`。 [备份配置](../ops/backup.md#配置) |
| FERTILE_BACKUP_REMOTE、FERTILE_BACKUP_RCLONE、FERTILE_BACKUP_WAL_BIN | 在 `config/backup.conf` 配置命名 `crypt` 目标、rclone 和 F3 CLI 的绝对路径。远端默认关闭。认证和加密材料放 `$FERTILE_ROOT/secrets/backup-rclone.conf`。 [异地备份](../ops/backup.md#私人-google-drive-加密备份) |
| FERTILE_BACKUP_WAL_PRODUCTS | 在 `config/backup.conf` 配置 F3 许可混合时的产品范围，以逗号分隔准确的 `source/product` 对。 [F3 配置](../ops/backup.md#远端文件与-f3-接口) |

控制器为 web 注入同一 release 的 WEB_STATIC_ROOT、finance HTTP 地址和 PostgreSQL socket。本机 web 使用独立的 fertile_web PostgreSQL 登录角色，通过 Unix socket peer 映射认证。运行时角色仅在其私有迁移 schema、app 和 blog 内具有相应权限，首次 SQLx 迁移的数据库 CREATE 权限由控制器临时授予并收回。管理员备份继续使用原 peer 身份。外部 DATABASE_URL 放在 `secrets/web-database.env`，使用外部连接时保留 `FERTILE_WEB_DATABASE` 默认值。单进程热升级由控制器持久化服务制品指针和事务，格式与恢复步骤见[热升级与本机接口](../deploy/hot-upgrade.md)。实例日常操作见 [普通服务器部署指南](../deploy/linux-server.md) 与 [Ruler 手册](../ops/ruler-runbook.md)。

重复准备受管库时复用带部署标记且符合权限约束的 `fertile_web` 角色，并保留角色 OID。遇到标记缺失或不符、角色属性不符合要求，或存在角色成员关系时，准备步骤会拒绝继续并保留原角色。集成演练分别检查旧库接管、受管身份复用和非受管角色拒绝。

本机只做本地压力测试和部署复验。保持 `FERTILE_PUBLIC_ORIGIN` 未设置，巡检覆盖文件中的 `public_origin` 省略或设为 `null`。公网采样记录为跳过，部署冒烟使用 `tests/e2e-smoke.sh local --paper-only`。未来的生产服务器由运维配置自己的 HTTPS origin。公网冒烟还需在 `tests/e2e-smoke.sh public` 后显式传入该 origin。[巡检与告警](../ops/monitoring.md)

## web 管理会话与集成凭据

以下变量可选，写在 `config/runtime.env`，由 supervisord 和 systemd 的控制器传给 web。未启用时省略对应行。修改后，supervisord 运行 `scripts/fertile ensure-up` 重新读取配置并替换配置有变的进程，systemd 重启 `fertile-web.service`。

| 参数 | 用途和默认行为 |
| --- | --- |
| `WEB_PUBLIC_ORIGIN` | 启用浏览器管理会话。填写与浏览器 `Origin` 完全一致的 HTTPS origin，省略默认端口、路径和尾部斜杠。未设置时浏览器管理会话关闭，已配置的 Bearer 令牌仍可用于支持它的接口。此设置独立于 `FERTILE_PUBLIC_ORIGIN` 公网巡检。 |
| `WEB_ADMIN_SESSION_TTL_SECONDS` | 会话期限，默认 900 秒，允许 1 至 900 秒。重启 web 后已有会话需要重新登录。 |
| `WEB_CREDENTIAL_DIR` | 与公钥路径成对设置。填写 web 运行账户所有的 0700 持久绝对目录，例如 `$FERTILE_ROOT/secrets/web-credentials`。目录须避开版本、静态资源及整栈备份采集路径。两项均省略时，集成凭据录入返回 503。 |
| `WEB_CREDENTIAL_RECIPIENT_PUBLIC_KEY_FILE` | 与密文目录成对设置。填写 web 可读的 RSA 公钥 PEM 绝对路径。公钥由 web 运行账户或 root 所有，组和其他用户不可写。私钥由获授权的接收方保管。 |

`runtime.env` 只放 origin、期限和路径等非机密值。启用凭据录入后，在 PostgreSQL 备份之外单独保管配套 JWE 密文和接收方私钥。权限、密钥轮换和备份说明见 [web 运行手册](https://github.com/viramoon/web/blob/90662dbb904724f119814576cb98128d8cb9ed05/RUNBOOK.md#集成配置及凭据)。

## 隔离历史验收的路径

本机 `runtime.env` 未设置 `FERTILE_RESEARCH_ROOT` 时，live API 与 worker 都读取 `$FERTILE_ROOT/data/research`。把验收日线导入该目录会立即让现网作业看到新快照。验收时仅为导入命令和 finance 隔离运行器指定专用研究根，使用独立绝对路径，避开 live 默认研究根及其子目录。

隔离运行器的数据根与研究根须为互不包含的实体绝对目录，专用队列写入验收数据根下的 `finance-state/backtests/jobs.sqlite3`，API 使用空闲的本机环回端口，避开 8081、8090、8082。手工归档 Yahoo、Ken French 和 FRED 时，为归档命令另行指定独立绝对路径 `FERTILE_DATA_ROOT`，公开历史版本写入该路径的 `catalog/historical`。执行步骤见 [隔离验收官方四窗口历史作业](../ops/ruler-runbook.md#6-隔离验收官方四窗口历史作业)。

## 凭据与轮换

| 文件 | 使用者与内容 |
| --- | --- |
| secrets/web.env | 可选。文件中只有 `WEB_ADMIN_TOKEN`。只读看板可匿名访问，提交回测等写操作需要 `Authorization: Bearer <token>`。 |
| secrets/web-database.env | 可选，web 专用的 DATABASE_URL。 |
| secrets/web-notifier.env | 启用 notifier 时配置固定 SMTP 中继、发件和收件地址及可选的凭据文件引用。notifier 与 web 使用同一 DATABASE_URL。 |
| secrets/ibkr-paper.env | 路径相对于 `$FERTILE_ROOT`。仅 Gateway 的 IBC 包装器读取 IBKR_USERNAME、IBKR_PASSWORD。健康检查只输出服务器时间和 DU 账户前缀。 |
| secrets/alerts.env | 可选 ALERT_WEBHOOK_URL。默认告警写本机 JSONL。 |
| 共享网站与隧道 | Maas · Ruler 管理现有网站、隧道、DNS 和凭据。fertile 只提供 127.0.0.1:8090 的源站。 |

创建 secrets 目录时使用 0700，凭据文件为服务属主的 0600 普通文件。运行时 env.sh 只提供工具链路径和缓存配置。备份排除凭据，Git 忽略 secrets/ 和本地 .env 文件。轮换时在盒子本地替换对应文件并重启相应服务，保留脱敏后的就绪和业务验证结果。详情见 [Gateway](../ops/ibgateway.md)、[监控](../ops/monitoring.md) 和 [管理令牌](../ops/ruler-runbook.md)。
