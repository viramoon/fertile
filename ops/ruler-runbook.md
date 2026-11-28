# Maas · Ruler 云盒子操作手册

常规发布依据已验证的 fertile tag。当前压测使用指定的 fertile 集成提交，其 gitlink 锁定 finance、web 和 frontend。所有命令从总仓库根目录运行。保留上一次可用的构建和备份，再切换代码或数据格式。本文的生命周期和灾备命令适用于 supervisord。Ubuntu 24.04 或 Debian 12 服务器的 systemd 安装、启动、卸载和 HTTPS 配置见 [普通服务器部署指南](../deploy/linux-server.md)。

本机只做本地压力测试。fertile 的入口为 `http://127.0.0.1:8090`，现有网站使用 `127.0.0.1:8080`。本机原有的 fertile 公网入口已移除，部署复验和巡检只走本地。Ruler 负责目标盒子部署、现有网站、Cloudflare 隧道、DNS 和线上恢复。fertile 的脚本只管理本项目进程和持久目录。部署不安装开机钩子、shell 启动项或主机定时任务。

在本机执行官方历史日线验收时，直接使用[部署后的隔离历史日线验收](#部署后的隔离历史日线验收)。

## 私有目录与验收记录

持久根和任何单独指定的行情、研究、数据库、导出、截图、日志及备份目录必须在四仓工作树外。配置前按 [数据发布政策](../docs/data-policy.md#私有目录) 检查实际路径。真实行情及授权证明保存在本机私有环境。GitHub 只记录获准公开的能力、价格及合成测试摘要，附件和 Actions 制品同样适用。真实账户验证由获授权人员在私有部署进行，仅使用 IBKR paper 只读权限。

## 首次启动

1. 以部署账户 checkout 选定的提交。初始化三个公开子模块及 Nautilus 源码，核对目标提交和 profile。
2. 按 [bootstrap](../deploy/bootstrap.md) 安装锁定的工具，执行 `source "$FERTILE_ROOT/env.sh"`。确认磁盘剩余足以构建 Rust、Python wheel、前端与备份。
3. 按 web 手册确认静态目录、数据库角色和管理令牌。凭据文件只允许属主读写。[web RUNBOOK](https://github.com/viramoon/web/blob/main/RUNBOOK.md)
4. 在本机 Gateway 完成 paper 登录前，独立记录本机服务状态、paper 账号和行情连续性。开发演练可显式选 replay，并为回放与真实行情分别安排持久数据根。

```sh
git submodule update --init --recursive
candidate=$(git rev-parse HEAD)
scripts/fertile build --ref "$candidate"
scripts/fertile ensure-up
scripts/fertile status --json
curl -fsS http://127.0.0.1:8090/api/v1/system/status
```

当前版本目录的 bin/ 下放编译产物，static/ 存放同一批次的 Vite dist。finance 与 web 各自读取版本目录内的配置，PostgreSQL、队列和 catalog 保留于持久数据目录。首次准备示例摘要时，加载同一个环境与数据根，再从当前 release 中执行以下命令。

`scripts/fertile data-root` 使用与 supervisor 相同的解析规则。已导出的 `FERTILE_DATA_ROOT` 优先，其次读取 `$FERTILE_ROOT/config/runtime.env`。使用独立的 paper 数据目录时，在该文件中填入实际绝对路径。两处都未设置时，IB 使用 `$FERTILE_ROOT/data`，replay 使用 `$FERTILE_ROOT/data/replay`。

~~~sh
source "${FERTILE_ROOT:?先设置实例的持久根}/env.sh"
(
  FERTILE_DATA_ROOT=$(scripts/fertile data-root) || exit
  export FERTILE_DATA_ROOT FINANCE_BUILD_ROOT="$FERTILE_ROOT/current/runtime"
  cd "$FERTILE_ROOT/current/finance" && PYTHONDONTWRITEBYTECODE=1 bash scripts/backtest-sample.sh
)
curl -fsS http://127.0.0.1:8090/api/v1/backtests/sample
~~~

## v1 阶段 0 重新部署

在维护窗口部署通过集成验收的 fertile 提交。该版本锁定 finance `b21f241`、web `90662db` 和 frontend `6eedc3d`。

| 再部署项目 | 操作 |
| --- | --- |
| 必需环境变量 | 无。四项可选的 web 设置及默认行为见 [web 管理会话与集成凭据](../docs/configuration.md#web-管理会话与集成凭据)。本机 HTTP 压测省略 `WEB_PUBLIC_ORIGIN`，继续使用已配置的 Bearer 令牌。 |
| 必需数据库迁移 | 执行新版 `web migrate` 应用 `20261126000000_integration_lifecycle.sql`，再启动新版 web。迁移在 `app` 创建集成配置、版本、凭据引用、测试和审计表。 |
| 前端制品 | 重新构建并用新版 web 托管完整的 `dist`。许可证文件随新脚本和样式一起切换，磁盘和 HTTP 路径见[第三方许可证复验](#第三方许可证复验)。 |
| finance 运行时变更 | 新增行情协议 crate 及 `export-market-protocol`、`finance-strategy` 两个源码工具，现有 Cargo `--bins` 构建覆盖它们。新增部署进程、端口、持久数据要求与 finance 数据迁移均为无。 |

1. 记录当前 fertile 提交、三个 gitlink 和实际 web 库名，暂停写入，保留旧版制品和可恢复的升级前备份。启用集成凭据录入的实例另存配套密文与接收方密钥。[备份与恢复](backup.md)
2. 检出获批提交并初始化递归子模块。运行 `scripts/fertile build --ref <fertile-commit>`，核对 `current/manifest.json` 的三个 gitlink 和完整静态目录。systemd 按普通服务器指南在构建前停旧 target，构建后重装指向新版的 unit。[systemd 版本切换](../deploy/linux-server.md#systemd)
3. 受管 PostgreSQL 由 supervisord 按[重启检查](#重启与外部入口)启动，或由 systemd 在启动时准备。两者均以 `fertile_web` 运行新版 `web migrate`，成功后启动新版 web。使用外部 PostgreSQL 时，由数据库管理员安排显式迁移。以 web 运行账户加载服务所用的 `DATABASE_URL` 和 profile，设置 `WEB_CONFIG_DIR="$FERTILE_ROOT/current/web/config"`，执行新版 `current/bin/web migrate`，成功后再重启到新版 web。确认 SQLx 台账已记录 `20261126000000` 成功。
4. 核对新版 web PID、`/healthz/ready` 和 `/api/v1/system/status`，打开终端页面并核对同源 JS/CSS，按[第三方许可证复验](#第三方许可证复验)核对两个第三方路径和根路径 `/NOTICE` 的响应。在部署复验窗口运行 `tests/e2e-smoke.sh local --paper-only`，确认可提交回测。官方历史日线作业按[隔离历史日线验收](#部署后的隔离历史日线验收)使用隔离队列。
5. 回退 W1 迁移时，将迁移前备份恢复到 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，在这对目标上运行旧发布。保留原数据、迁移台账和密文。旧版 SQLx 迁移器会拒绝台账中的新增迁移。跨越 finance `7600be4` 时，还要确认该快照早于首次写入旧版无法读取的四窗口摘要。[从升级前快照回滚并切回新版](#从升级前快照回滚并切回新版)

### 许可证路径修复升级

从 fertile `8e49834` 升级到锁定 web `de02f75` 的制品时，服务变更只需部署新版 web 二进制并重启 web。选用已通过集成验收的完整 fertile 制品。若切换发布目录，确保守护器加载指向新版的路径。supervisord 需重载进程配置，systemd 按[版本切换流程](../deploy/linux-server.md#systemd)重新安装固定到新版的 unit。静态制品布局继续使用 `$release/static/`，沿用原有数据库迁移、环境变量、端口和进程配置。重启后核对新版 web PID、`/healthz/ready` 和 `/api/v1/system/status`，再核对本机许可证响应。

### 第三方许可证复验

`$release` 是部署中的发布目录，`WEB_STATIC_ROOT` 指向其 `static/` 子目录。前端使用 `/third-party/lightweight-charts/LICENSE` 和 `/third-party/lightweight-charts/NOTICE`，根路径 `/NOTICE` 提供同一份 NOTICE。三个 URL 均应返回 HTTP 200，正文分别与 `$release/static/third-party/lightweight-charts/{LICENSE,NOTICE}` 对应文件逐字节一致。根路径对应的完整制品路径为 `$release/static/third-party/lightweight-charts/NOTICE`。

在目标机器设置 `release` 为实际发布目录，并使用本机 web 端口运行以下核对。误写的 `/static/third-party/lightweight-charts/LICENSE` 和 `/static/third-party/lightweight-charts/NOTICE` 均应返回 404。

```bash
(
  set -eu
  origin=http://127.0.0.1:8090
  license_dir="${release:?先设置实际发布目录}/static/third-party/lightweight-charts"
  body=$(mktemp)
  trap 'rm -f -- "$body"' EXIT
  for path in /third-party/lightweight-charts/{LICENSE,NOTICE} /NOTICE; do
    status=$(curl --noproxy '*' -sS -o "$body" -w '%{http_code}' "$origin$path")
    printf '%s HTTP %s\n' "$path" "$status"
    test "$status" = 200
    cmp -- "$body" "$license_dir/${path##*/}"
  done
  for path in /static/third-party/lightweight-charts/{LICENSE,NOTICE}; do
    status=$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' "$origin$path")
    printf '%s HTTP %s\n' "$path" "$status"
    test "$status" = 404
  done
)
```

## v1 T3b 第 2 批运行时核对

换版使用同一 fertile 提交构建的 `finance-api`、worker、`finance-strategy`、`web`、`web-notifier` 与完整 frontend `dist`。维护前按[受监管启停与手动排空](#受监管启停与手动排空)暂停接入，排空回测并保留已验证的备份。记录实际数据根、研究根、目标 PostgreSQL 库、迁移台账和旧制品。[T3b 集成说明](../docs/v1-integration.md)

| 核对项 | 第 2 批操作 |
| --- | --- |
| PostgreSQL | web 的 SQLx 迁移终点为 `20261127074531`（W5 主动静默）。从 T3a 升级无新增 SQL 文件。核对已应用记录、校验和及数据库就绪，从更早版本升级时在新版业务进程启动前显式执行 `web migrate`。 |
| 回测 SQLite | F14 在 finance API 处理作业请求或 worker 启动时运行队列建表脚本并建立 `jobs_legacy_id` 部分索引。备份 `finance-state/backtests/jobs.sqlite3`，保留其中的作业、策略版本、快照和租约。切换后核对队列复用、配置回测结果及匿名分页。匿名页只包含旧版公开作业，游标指向该页最后一个可见 ID。worker 使用同版 `FINANCE_STRATEGY_BIN` 和既有数据根。 |
| 因子报告 | 管理员从 web 读取 finance 在 `FERTILE_DATA_ROOT` 下已发布的 `finance-state/factor-research/read-v1.json`。缺少发布快照时列表为空，`as_of` 为 `null`。核对数据根与快照版本，缺失数据时继续标记为不可用。[finance 策略与研究契约](https://github.com/viramoon/finance/blob/86dcc8604b6881d648418a25e6cd13c24d4a9b01/docs/api.md#第二批策略回测与研究契约) |
| 环境、端口和进程 | W2b、W8、W9 共用既有 `FINANCE_BASE_URL`、同源静态目录、端口和进程。默认为 web `8090`、finance API `8081`、collector `8082`，均监听回环。HTTPS 浏览器仍设置 `WEB_PUBLIC_ORIGIN`。省略该配置时，仅接受 `Host` 匹配的同源 HTTP 回环管理会话。独立 finance API 可用已有的 `FINANCE_BINANCE_CONFIG` 选择状态读取所用的 profile 文件，默认读取发布内 profile。此批部署无需新增必填环境变量或独立卷。[运行配置](../docs/configuration.md) |
| 行情源与 F3 | 成功读取链、场所深度、敞口及期权流需要获授权的 IBKR paper 只读行情所有者接入。独立 `finance-api` 的这些来源未装配时会经 web 返回 `503 finance_unavailable`。有来源时按已知合约和时窗核对许可、报价时间、覆盖及缺口。OPRA 历史原始证据仅在获准本机记录时写入 `finance-state/market-wal/ibkr/opra-history`。F3 数据根与投影、目录、文件均使用工作树外的实体路径，核对符号链接和路径类型。[finance 读契约](https://github.com/viramoon/finance/blob/86dcc8604b6881d648418a25e6cd13c24d4a9b01/docs/api.md#行情所有者接入) · [F3 投影路径](https://github.com/viramoon/finance/blob/86dcc8604b6881d648418a25e6cd13c24d4a9b01/docs/market-wal.md#parquet-与投影游标) |
| 默认关闭 | 核对 `FERTILE_ENABLE_FINANCE_PROJECTOR=0`、`FERTILE_ENABLE_WEB_NOTIFIER=0`。Binance、Hyperliquid、额外 worker、实时决策模板和 Databento 维持关闭。私人 Drive 远端保持关闭，待核准许可、账户、容量和密钥后独立启用。 |
| 邮件 | `FERTILE_ENABLE_WEB_NOTIFIER=0` 时守护器不启动 `web-notifier`，状态为 `disabled` 且 PID 为 `0`。W11 的独立二进制支持停用或缺少可用 SMTP 配置时安全空转。W13 在运行期间找不到候选绑定快照时保留该测试为 `pending`，恢复原文件后按额度及退避重试。当前总仓的受管 notifier 使用 W3 固定中继，W4 绑定快照环境和停止宽限（上游要求至少 85 秒）需先完成总仓接线与复验。[web 邮件运行手册](https://github.com/viramoon/web/blob/fcfda737d5152518d1d77cd55a5287c7185f3fb7/RUNBOOK.md#告警与邮件进程) |

回退前暂停 notifier，清点 outbox。凡含新增迁移的发布，把迁移前备份恢复到独立 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，再在这对目标上运行旧发布。旧代码不得连接新版写过的数据库。保留新版数据和台账，恢复后核对实际连接、队列和采集水位。切回新版时恢复原目标并对账。[隔离回退步骤](#从升级前快照回滚并切回新版)

## v1 T3a 首批运行时核对

下列核对用于首批 T3a 制品。维护前备份 web 库、回测队列和已授权数据，记录旧制品与数据根。U6 的生成客户端锁定 web W6，选用同一发布的 web 和前端 dist。[T3a 集成说明](../docs/v1-integration.md#t3a-首批验收)

| 项目 | 操作 |
| --- | --- |
| web 与前端 | 构建 `web`、`web-notifier` 与完整前端 `dist`。W6 沿用迁移终点 `20261127074531`，切换前确认同一 web 库已应用。受管库由现有准备流程迁移，外部库由管理员显式执行。既有端口继续提供 HTTP、SSE 和 WebSocket。按实际入口复验市场流和系统页的质量水位。[WebSocket 入口核对](#websocket-入口核对) |
| finance 与 F3 | 采集和 API 使用原入口。F6 bars 与指标 REST 读取受控投影视图。F3 的 `finance-market-wal` 与 Python 投影随候选制品构建。启用投影用 `FERTILE_ENABLE_FINANCE_PROJECTOR=1`，核对产品记录许可、单写者和 WAL/投影水位。保留旧 bar catalog 与队列。 |
| F4 与 F12 | `bin/finance-binance` 和两种关闭的 profile 随发布交付，独立服务模板默认关闭。启用前核对产品许可、`FINANCE_BINANCE_CONFIG`、专用数据根和退避策略。F12 是现有 finance 进程内的期权历史库，借用 paper 只读会话和历史任务节流；Databento 默认关闭。 |
| F13 worker 与决策 | 先排空并停止旧 worker，保留 SQLite 队列及锁文件。现有 `finance-backtest-worker` launcher 启动 F13，在既有队列中自动创建 `job_leases`，租约默认 15 秒。配置策略求值使用同发布的 `FINANCE_STRATEGY_BIN`。额外 worker 与 `finance-live-decision` 模板默认关闭。独立 `finance/scripts/finance-decision` 使用明确的 `--plan`、`--replay` 与绝对数据根，将目标和检查点写入本地决策库。 |
| 邮件 | `FERTILE_ENABLE_WEB_NOTIFIER=1` 时由守护器启动 `web-notifier`。与 web 共用已迁移数据库，无监听端口。先用专属 `web-notifier.env` 配好固定 SMTP 中继、发件地址、本人收件地址和可选凭据文件引用。启用前用隔离 outbox 复验状态。[进程条件](../deploy/hot-upgrade.md#宿主与持久目录) |
| 备份 | offsite 默认关闭。仅在确认产品云端备份许可、接收方账户与离线密钥安排后配置 T2b，接入 F3 时显式设置 `FERTILE_BACKUP_WAL_BIN`，按产品授权选择封段。远端和收件人由 vira 决定。[备份与恢复](backup.md#私人-google-drive-加密备份) |

F3 投影和 W3 notifier 默认关闭。`FERTILE_ENABLE_WEB_NOTIFIER=0` 时守护器不启动 `web-notifier`。在应用该配置后，核对 `status --json` 中 notifier 为 `disabled` 且 PID 为 `0`。T3a 验证 W3 邮件 worker 组件，进程清单使用 W3 固定中继设置。Web W11 另行说明直接启动其新版二进制时，显式禁用或缺少可用 SMTP 配置如何安全空转、保留 outbox 且不发送邮件。[web W11 notifier 禁用说明](https://github.com/viramoon/web/blob/be78e8de19754f728710e21294c59fcda6aeb997/RUNBOOK.md#告警与邮件进程)

IBKR 使用 paper 只读行情。九标的回放与现网实例使用独立数据根。进程环境、默认值和独立运行边界见 [finance 配置](../docs/configuration.md#finance-独立程序参数)。

回退前先暂停 notifier，清点待发与已静默记录。含新增迁移的发布一律把迁移前备份恢复到 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，再在这对目标上运行旧发布。保留升级后的 PostgreSQL、已授权 WAL 和回测结果。投影可从所选恢复点的已封段 WAL 在隔离根重建。[跨版本恢复](#从升级前快照回滚并切回新版) · [单写者交接](../deploy/hot-upgrade.md)

### WebSocket 入口核对

按实际客户端入口和路由判断。先比对 WS 请求的主机、端口和连接路径与运行中 web 的监听地址，再查入口配置或路由记录。客户端直接连接 web 的回环监听端口（本机默认 `127.0.0.1:8090`），且中途无 HTTP 反向代理，属于直连。即使上游最终也指向回环端口，只要请求先经过 nginx、Caddy 或隧道路由等反向代理，就按反代入口核对。

- **前置反向代理时**，才核对代理转发的 WebSocket 升级头和 `Origin`。确认到 web 的 HTTP/1.1 上游保留 `Upgrade: websocket`、`Connection: Upgrade` 的语义，并原样传递浏览器 `Origin`。使用已配置的 `WEB_PUBLIC_ORIGIN` 管理会话从实际入口连接 `/api/v1/stream/market`，核对握手返回 `101 Switching Protocols`，收到预期市场帧并能断线重连。
- **直连回环端口时**，跳过代理升级头和 `Origin` 转发核对。使用与该监听端口同一入口的 `/healthz/ready` 和 `/api/v1/system/status` 核对 web 与 finance，再用支持现有鉴权方式的 WebSocket 客户端直连 `/api/v1/stream/market`，确认 `101 Switching Protocols`、预期市场帧及断线重连。非浏览器客户端可用获授权的 Bearer 凭据发起省略 `Origin` 的原生握手。未配置 `WEB_PUBLIC_ORIGIN` 时，T3b 的浏览器管理会话使用同源 HTTP 回环地址，握手仍须满足 web 的应用层 `Origin` 校验。[web 行情网关说明](https://github.com/viramoon/web/blob/fcfda737d5152518d1d77cd55a5287c7185f3fb7/RUNBOOK.md#行情网关与运维事件)

## 部署后的隔离历史日线验收

**本机验收禁令。** 不修改 `runtime.env` 或 `env.sh`，不重启或重指 live API / worker，不对 `data-paper` 或 live 库 `web` 做正式 restore。本机不要运行 `scripts/fertile up`、`ensure`、`ensure-up`、`down` 或 `rollback`，这些入口会调用 ensure、启动 healthwatch 或改变 live 服务状态。不要通过 `/backtests` 页面或 8090 提交，也不要执行 `tests/e2e-smoke.sh local --research-instrument …`。这些入口写入 live 队列 `data-paper/finance-state/backtests/jobs.sqlite3`，冒烟还会先提交示例作业。

**默认研究根属于 live。** `runtime.env` 未设置 `FERTILE_RESEARCH_ROOT` 时，`export FERTILE_RESEARCH_ROOT="$FERTILE_ROOT/data/research"` 指向 live API / worker 的默认研究根，往该路径或其下写入的验收数据会被现网作业看见。验收导入只用本次新建的独立绝对路径。[运行配置](../docs/configuration.md)

### 1. 确认部署版本并建立独立目录

在专门的 Bash 会话中逐段执行，每条命令成功后再继续。以下示例使用已部署且 manifest 锁定目标 finance 提交的制品。若校验失败，停止并请 Ruler 提供对应制品。源码构建方式见 finance 原手册。两个隔离根都要存在、互不包含，路径各级使用实体目录。[finance 隔离入口](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/RUNBOOK.md#隔离验收官方历史作业) [finance 作业说明](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/docs/backtest-jobs.md#隔离运行官方历史作业)

```bash
umask 077
FERTILE_ROOT=${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}
release=$(readlink -f -- "$FERTILE_ROOT/current") || exit
grep -F '"finance": "b21f24150dae968a147b57eefbe4ffde0f3b4809"' "$release/manifest.json" || exit
cd "$release/finance" || exit
export FINANCE_PYTHON="$release/runtime/venv/bin/python"
export PYTHONPATH=python PYTHONDONTWRITEBYTECODE=1
acceptance_root=$(mktemp -d /tmp/fertile-historical.XXXXXXXX) || exit
export FINANCE_ISOLATED_DATA="$acceptance_root/job-data"
export FERTILE_RESEARCH_ROOT="$acceptance_root/research"
export FINANCE_HISTORY_ARCHIVE="$acceptance_root/archive"
mkdir -p -- "$FINANCE_ISOLATED_DATA" "$FERTILE_RESEARCH_ROOT" "$FINANCE_HISTORY_ARCHIVE" || exit
```

### 2. 下载并记录公开历史数据

确认来源允许下载、使用和本地归档。Yahoo 使用 finance 的 `download_yahoo_daily`，默认提供明确的 User-Agent。遇到 429、408、5xx 或连接失败时，按退避策略在 query1 和 query2 间切换并遵守 `Retry-After` 的等待预算。失败时保留脱敏错误，按供应商要求稍后重试。下面的 IBM 日期仅供演示，旧 SPY 的下载参数见下一节。记录获取日期、来源、实际首尾、条数和缺口。Ken French（含 FF5 下载示例）、FRED 及公开历史 catalog 导入命令见原手册。[finance 手工归档步骤](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/RUNBOOK.md#手工保存公开历史数据)

```bash
"$FINANCE_PYTHON" -m fertile_finance.download_yahoo_daily \
  --symbol IBM --start 2024-01-01 --end 2024-12-31 \
  --output "$FINANCE_HISTORY_ARCHIVE/yahoo-IBM.json"
```

如需保留公开历史 Parquet，先为原手册的归档导入块设置 `export FERTILE_DATA_ROOT="$acceptance_root/history-data"`，并将 `FINANCE_YAHOO_AS_OF` 设为 Yahoo 存档实际观测截止日。公开历史版本写入该数据根的 `catalog/historical`。采集 catalog 备份会拒绝其中的 `current` 符号链接。确认归档停写后，单独备份原始文件、`current` 和它指向的版本目录。

### 3. 单独备份并迁移旧 SPY

本机 `scripts/backup.sh` 的行情与队列备份范围为活动 `FERTILE_DATA_ROOT`（`data-paper`）。旧 SPY 固定 21:00 UTC 日线在非活动 `$FERTILE_ROOT/data/catalog/data/bar/SPY.XNAS-1-DAY-LAST-EXTERNAL`，对应 equity 在同一 catalog 的 `data/equity/SPY.XNAS`，两者均不在日常快照内。确认旧 catalog 已无写入方后，先将日线和 equity 复制到独立持久目录，再对副本迁移。原件保留并保持只读。[单独备份说明](backup.md#非活动旧-spy-catalog-的单独备份)

```bash
legacy_catalog="$FERTILE_ROOT/data/catalog"
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

在 `$legacy_backup` 记录来源目录和提供方、标的 `SPY.XNAS`、实际首尾和条数、原消费方、固定 21:00 UTC 时间口径、OHLCV 和复权口径。下载覆盖旧交易日的 SPY 源 OHLCV、调整收盘价和公司行为。输入核实后的旧归档首尾，以及新 Yahoo 存档的实际观测截止日。finance 迁移示例默认使用 ARCA Bar 类型，本机按示例指引通过 `--help` 选用 `--bar-type SPY.XNAS-1-DAY-LAST-EXTERNAL`。[finance SPY 迁移说明](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/catalog/README.md#既有-spy-固定-2100-utc-数据)

```bash
read -r -p '旧 SPY 首日 (YYYY-MM-DD): ' spy_first_session
read -r -p '旧 SPY 末日 (YYYY-MM-DD): ' spy_last_session
"$FINANCE_PYTHON" -m fertile_finance.download_yahoo_daily \
  --symbol SPY --start "$spy_first_session" --end "$spy_last_session" \
  --output "$FINANCE_HISTORY_ARCHIVE/yahoo-SPY.json"
read -r -p 'Yahoo SPY 实际观测截止日 (YYYY-MM-DD): ' spy_as_of
spy_target="$acceptance_root/spy-data"
"$FINANCE_PYTHON" -B -m fertile_finance.migrate_spy \
  --source-catalog "${legacy_backup:?}/catalog" \
  --bar-type SPY.XNAS-1-DAY-LAST-EXTERNAL \
  --target-data-root "$spy_target" \
  --yahoo-json "$FINANCE_HISTORY_ARCHIVE/yahoo-SPY.json" --adjustment-as-of "$spy_as_of" \
  --dry-run > "$acceptance_root/spy-dry-run.json"
```

核对 dry-run 的新旧首尾、条数、缺口、全日市冬令时 21:00 UTC、夏令时 20:00 UTC 及半日市实际收盘时间，并核对源价精度、复权因子及公司行为。确认后，用同一组参数去掉 `--dry-run`。

```bash
"$FINANCE_PYTHON" -B -m fertile_finance.migrate_spy \
  --source-catalog "${legacy_backup:?}/catalog" \
  --bar-type SPY.XNAS-1-DAY-LAST-EXTERNAL \
  --target-data-root "$spy_target" \
  --yahoo-json "$FINANCE_HISTORY_ARCHIVE/yahoo-SPY.json" --adjustment-as-of "$spy_as_of" \
  > "$acceptance_root/spy-migration.json"
```

核对重导报告及目标 `$spy_target/catalog/historical/yahoo/SPY.ARCA`。如需保留新归档，将完整版本转存到独立的持久研究位置，保留旧副本和两个报告。官方作业输入按第 5 步审核，SPY 迁移后的 `SPY.ARCA` 归档供单独研究读取。

### 4. 可选重拉 FRED 对齐旧 SPY

**官方四窗口作业不使用 FRED。** 无需 FRED series ID、`cosd` 或 `coed`。已导入的 2024 年 `DGS10` 共 261 条，其覆盖与验收无关。官方作业也无需 Ken French 日频，Sharpe 固定 `rf=0`，结果标记为 `rf_source="rf=0"`。

独立研究如需把 `DGS10` 对齐旧 SPY 最长样本，按 finance 手册的可选命令设置 `cosd=1993-01-29`，将 `coed` 填为核实后的 SPY 归档实际末日。下例的末日仅演示。下载后核对日频、年化百分比单位、实际首尾和缺值，再导入新的独立数据根。若要保留该研究归档，在验收清理前将其转存到独立的持久位置。[finance 可选重拉及导入](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/RUNBOOK.md#可选扩展-fred-归档以对齐-spy-样本)

```bash
export FINANCE_FRED_FIRST_DATE=1993-01-29
export FINANCE_SPY_LAST_SESSION=2024-12-31  # 改为核实的 SPY 归档末日
export FINANCE_FRED_DATA_ROOT="$acceptance_root/fred-spy-span"
export FINANCE_FRED_SPY_CSV="$FINANCE_HISTORY_ARCHIVE/fred-DGS10-spy-span.csv"
curl --fail --location --get 'https://fred.stlouisfed.org/graph/fredgraph.csv' \
  --data-urlencode 'id=DGS10' --data-urlencode "cosd=$FINANCE_FRED_FIRST_DATE" \
  --data-urlencode "coed=$FINANCE_SPY_LAST_SESSION" --output "$FINANCE_FRED_SPY_CSV"
```

下载成功并核对归档后，沿用以上变量按 finance 原文导入，核对写入结果和 `Missing DGS10 values`。

```bash
"$FINANCE_PYTHON" - <<'PY'
from datetime import date
import os
from pathlib import Path

from fertile_finance.historical_catalog import HistoricalCatalog
from fertile_finance.research_factors import load_fred

series = load_fred(Path(os.environ["FINANCE_FRED_SPY_CSV"]), series_id="DGS10")
catalog = HistoricalCatalog(Path(os.environ["FINANCE_FRED_DATA_ROOT"]))
start = date.fromisoformat(os.environ["FINANCE_FRED_FIRST_DATE"])
end = date.fromisoformat(os.environ["FINANCE_SPY_LAST_SESSION"])
print(catalog.write_fred(series, start=start, end=end))
print("Missing DGS10 values:", sum(row.value is None for row in series.rows))
PY
```

### 5. 转换并审核导入研究快照

使用经许可的 NYSE 上市 USD 股票或 ETF 日线，确保 XNYS 会话连续、成交量为整股，并排除现金分红复权。下例使用经审核的无拆股原始 OHLCV，从 Yahoo 源 quote 生成 CSV 和血缘。转换器接受与最近美分的绝对差不超过 `0.00002 USD` 的 OHLC 报价，将其归一到美分，并以 `normalized_price_count` 记录数值改变的 OHLC 字段数。超出容差时转换会退出。导入前核对来源、价格口径、公司行为、实际首尾、条数、缺口、`adjustment_as_of` 和 CSV SHA-256。若输入已在 `HistoricalCatalog`，按 finance 文档使用 `--historical-data-root` / `--series-id`。[finance 日线准备和导入](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/docs/historical-backtest.md#准备数据)

```bash
"$FINANCE_PYTHON" -m fertile_finance.convert_research_csv \
  --yahoo-json "$FINANCE_HISTORY_ARCHIVE/yahoo-IBM.json" --adjustment-as-of 2024-12-31 \
  --start 2024-01-02 --end 2024-12-31 --output "$FINANCE_HISTORY_ARCHIVE/ibm.csv" \
  > "$FINANCE_HISTORY_ARCHIVE/ibm.lineage.json"
cat "$FINANCE_HISTORY_ARCHIVE/ibm.lineage.json"
sha256sum "$FINANCE_HISTORY_ARCHIVE/ibm.csv"
```

日期按已核实的源文件取值。审查通过后，把快照导入第 1 步的 `$FERTILE_RESEARCH_ROOT`。

```bash
"$FINANCE_PYTHON" -m fertile_finance.research_catalog "$FINANCE_HISTORY_ARCHIVE/ibm.csv" \
  --instrument IBM.XNYS --asset-type stock --kind historical \
  --source "Yahoo Finance chart IBM" --raw-split-free-reviewed
```

跨拆股时，先用供应商资料、公司行为记录和拆股日前后的独立样本审核，确认 Yahoo 已将 OHLCV 统一到同一拆股后股份口径，并核对从请求起点到存档截止日的事件。使用同一 `adjustment_as_of` 的行情，转换时传入 `--yahoo-split-normalized-evidence` 记录审核依据。转换器保留最后一个相关拆股日前的源报价精度，自该日起应用美分容差。核对 CSV 和血缘后，以 `--yahoo-split-normalized-lineage` 导入。此选项与上例的 `--raw-split-free-reviewed` 互斥，证据不足时将验收记为未验证。经审核的无拆股供应商原始 CSV 也可用于上例的原始入口，`--source` 填实际来源。禁止合成或补造数据、擅自舍入或改价。已发布的标的目录不可覆盖，重试导入用新的独立研究根。[finance 跨拆股审核和命令](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/docs/historical-backtest.md#跨拆股的-ibm-长历史)

### 6. 隔离验收官方四窗口历史作业

按已导入标的的覆盖范围调整日期和标的。请求区间至少包含 `slow_period + 1` 个连续 XNYS 会话，示例使用默认慢线 30 日。示例端口 18081 必须空闲，可省略 `--port 18081` 让运行器自动选高位端口。避开 8081、8090 和 8082。运行器启动本次专用的环回 finance-api 和 worker，提交一个 `SmaCrossover` 并轮询终态，队列为 `$FINANCE_ISOLATED_DATA/finance-state/backtests/jobs.sqlite3`。运行结束或中断后回收本次进程并释放端口。[finance 官方隔离命令](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/RUNBOOK.md#隔离验收官方历史作业)

```bash
"$FINANCE_PYTHON" -m fertile_finance.isolated_historical_job \
  --api-bin "$release/bin/finance-api" \
  --data-root "$FINANCE_ISOLATED_DATA" --research-root "$FERTILE_RESEARCH_ROOT" \
  --port 18081 --instrument IBM.XNYS --start 2024-01-02 --end 2024-12-31 \
  > "$acceptance_root/job.json" 2> "$acceptance_root/job.stderr"
```

退出码为零后检查 `job.json` 的 `status=succeeded`、请求和摘要策略均为 `SmaCrossover`，标的与日期匹配本次请求，`summary.research.dataset` 与快照清单一致。非零时保留错误、终态 JSON（若有）和隔离队列供诊断。

- 核对 `summary.research.windows` 依序为 `50y`、`10y`、`5y`、`longest_sample`，`requested_years` 分别为 50、10、5、`null`。记录每行请求起点、实际及日历覆盖首尾、观测数、应有和缺失交易日、覆盖状态及 `rf_source`。覆盖不足时 `metrics=null`，有值的绩效使用十进制字符串。示例的一年区间应使三个固定窗口覆盖不足，最长样本按 warmup 后的实际净值序列核对。
- 核对 `summary.research.audit.fills` 等于 `summary.total_orders`。抽查 `recent_fills` 的信号日、执行日与 `executed_at`，确认在下一 XNYS 会话收盘成交，股数、价格和佣金与配置及模拟规则相符。记录零量 bar、阻塞计数和成交样本。零成交时保留计数，需验证逐笔时选经审核且能触发成交的区间重跑。[finance 成交与绩效口径](https://github.com/viramoon/finance/blob/b21f24150dae968a147b57eefbe4ffde0f3b4809/docs/historical-backtest.md#信号执行和绩效)

### 7. 留存证据并清理

记录 UTC 时间、release manifest、数据与研究根、端口、来源和数据覆盖、作业 ID、四窗口检查结果及成交审计。保留终态 JSON、stderr、CSV 血缘和 `$FERTILE_RESEARCH_ROOT/IBM.XNYS/dataset.json`。若使用供应商 CSV，保存对应审核记录。若进行了 SPY 迁移或 FRED 归档，一并保留报告与归档记录。需要留存队列时，在运行器退出后从独立数据根复制队列目录。以下示例把由 Yahoo 转换输入且成功的 IBM 作业证据留在临时目录之外。

```bash
evidence=$(mktemp -d "$HOME/fertile-historical-evidence.XXXXXXXX")
cp -- "$release/manifest.json" "$acceptance_root/job.json" "$acceptance_root/job.stderr" \
  "$FERTILE_RESEARCH_ROOT/IBM.XNYS/dataset.json" "$evidence/"
cp -- "$FINANCE_HISTORY_ARCHIVE/ibm.lineage.json" "$evidence/"
sha256sum "$FINANCE_HISTORY_ARCHIVE/ibm.csv" > "$evidence/ibm.sha256"
printf '%s\n' "$evidence"
```

确认运行器已退出、专用端口已释放、证据和需要保留的归档均已转存，再删除第 1 步创建的临时根。只使用本次 `mktemp` 返回的路径。单独备份的旧 SPY 保留在持久备份目录。

```bash
if [[ ${acceptance_root:-} =~ ^/tmp/fertile-historical\.[A-Za-z0-9]{8}$ && ! -L "$acceptance_root" ]]; then
  rm -rf -- "$acceptance_root"
fi
```

## 启停、诊断与回滚

可按发布范围选择[单进程热升级](#单进程热升级与回退)或[受监管启停与手动排空](#受监管启停与手动排空)。后者在维护窗口切换整栈，接受服务短时中断，适用于新增迁移、需要调整进程或卷的发布，也可用于没有已验证热切换 hook 的发布。

### 单进程热升级与回退

保留已通过隔离回读的旧版制品。从总仓检出暂存下一发布，确认前后两版可读取现有数据，按应用角色逐个切换。升级 finance API 或 web 时核对 collector 的 PID、水位和来源。切换采集器前准备同源封段、租约交接和缺口检查。使用 `hot-upgrade-status` 查询事务，遇到控制器意外退出或切换结果不确定时，先按实际进程、端口、水位和租约核对，再记录恢复结果。复核、命令和接口见[热升级与本机接口](../deploy/hot-upgrade.md)。

含新增迁移的发布一律按[隔离恢复流程](#从升级前快照回滚并切回新版)回退。旧代码不得连接新版写过的数据库。整栈命令用于维护窗口，由获授权的部署账户在目标主机执行。开发与独立复测使用临时目录和合成或回放 fixture。

### 受监管启停与手动排空

1. **准备维护窗口。** 确定唯一的进程守护器、允许中断的时段和排空截止时间。记录两版 fertile 提交及三个 gitlink、实际服务 PID、数据根、研究根、web 库名、迁移台账、行情来源与水位。保留旧版完整制品，用 `scripts/fertile build --stage-only --ref "$candidate"` 暂存已通过隔离验收的完整候选，提前确认可恢复备份。核对热升级状态，先解决未完成的交接事务。挂起可能重启服务或继续写入的巡检、自启和调度，核对 notifier 的实际启用状态。
2. **先排空 worker。** 在入口封住新回测提交，覆盖浏览器、提交脚本、定时任务和可直连 finance API 的提交方。确认在途提交均已结算，保持旧 worker 运行，由它处理现有 `queued` 和 `running` 作业。在已确认的实际数据根，对 `finance-state/backtests/jobs.sqlite3` 使用只读 SQLite 连接，例如 `sqlite3 -readonly`。先按旧制品和实际 schema 核对队列版本，可用 `SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'job_leases'` 查租约表。存在 `job_leases` 表时执行以下查询，确认 `queued`、`running`、租约均为 `0`，且没有在途引擎子进程。

   ```sql
   SELECT (SELECT count(*) FROM jobs WHERE status = 'queued') AS queued,
          (SELECT count(*) FROM jobs WHERE status = 'running') AS running,
          (SELECT count(*) FROM job_leases) AS leases;
   ```

   确认旧发布未采用 F13 且无 `job_leases` 表时，执行以下旧队列查询。两个计数均为 `0` 且没有在途引擎子进程才算排空。若旧制品应有租约表但实际缺失，保持维护状态并核对队列版本。

   ```sql
   SELECT (SELECT count(*) FROM jobs WHERE status = 'queued') AS queued,
          (SELECT count(*) FROM jobs WHERE status = 'running') AS running;
   ```

   记录最后一次核对时间和计数。超时或有孤立作业、租约时暂停换版，按旧版作业恢复流程核对进程与锁，保留队列和锁文件。[finance worker 运行与恢复](https://github.com/viramoon/finance/blob/fc5e153e9727181f5748659c0610b2fbc2b49769/docs/backtest-jobs.md#worker-运行与恢复)

3. **确认排空后才由守护器停止。** 对已启用的 notifier，先清点待发及已静默项，确认停止宽限覆盖当前尝试的结算时限。采集器若支持交接，在停机前按源记录封段截点和最后水位。supervisord 实例先执行 `scripts/fertile stop finance-backtest-worker`，核对原 worker 与引擎均已退出，再用 `scripts/fertile down` 停整栈。systemd 实例先执行 `sudo systemctl stop fertile-finance-backtest-worker.service`，核对该 unit 已停止且无残留引擎，再用 `sudo systemctl stop fertile.target` 停整栈。若另有受管 worker 共用队列，逐一核对并在停整栈前停止。停栈后确认 notifier 的在途尝试已结算，并核对旧采集器已退出、写者锁及源订阅已释放，再允许新采集器接手。IBKR 继续使用 paper 只读，保持唯一行情订阅拥有者。[采集交接与缺口](../deploy/hot-upgrade.md#应用-hook-协议-v1)
4. **切换并受监管启动。** 业务写入停止后按[备份手册](backup.md)保留迁移前快照，含旧队列、web 库和所需配套文件。备份受管库需要 PostgreSQL 时单独启动数据库。对快照运行 `scripts/restore.sh <快照> --dry-run`。在停栈状态归档逐服务发布指针，用 `scripts/fertile build --ref "$candidate"` 验证并启用已暂存制品，核对 `current` 与 manifest 中的三个 gitlink。

   含新增迁移时，在启动新版业务进程前单独执行新版 `web migrate`，并核对目标库的 SQLx 台账。此时只运行 PostgreSQL，保持 finance API、collector、worker、web 和 notifier 停止。

   - 受管 PostgreSQL 由部署账户核对原实例根、实际端口和目标库。若备份阶段已单独启动 PostgreSQL，先停止该进程。用候选制品的 `fertile/scripts/postgres.sh foreground` 单独启动 PostgreSQL，设置 `FERTILE_WEB_PEER=1` 并传入该实例的 `FERTILE_WEB_DATABASE` 和 `FERTILE_POSTGRES_PORT`。连接 URI 使用 `fertile_web`、实际库名与端口，并将 `$FERTILE_ROOT/run/postgres` 做 URL 百分号编码后作为 `host` 查询参数。若旧库仍需准备角色、权限或 SQLx 搜索路径，由数据库管理员先按候选制品的 `fertile/scripts/prepare-web-db.sql` 准备并核对目标。
   - 外部 PostgreSQL 由 DBA 确认目标库、迁移角色和权限。两类库均使用部署核准的 `DATABASE_URL` 和 profile，设 `WEB_CONFIG_DIR="$FERTILE_ROOT/current/web/config"`，运行 `"$FERTILE_ROOT/current/bin/web" migrate`。核对退出码与该库台账中候选制品要求的版本及成功状态。

   supervisord 的 web 准备和 systemd 的 web `ExecStartPre` 都在 finance 服务启动阶段之后轮到 web 时执行。迁移或台账核对失败时保持业务进程停止，按第 6 步处理。计划恢复邮件时，在守护器启动 notifier 前核对 outbox、目标库和制品。投递状态尚待对账时以 `FERTILE_ENABLE_WEB_NOTIFIER=0` 启动。迁移及台账核对通过后，先用 `scripts/postgres.sh stop` 停止本次单独启动的受管 PostgreSQL，再交回守护器。supervisord 实例按[重启检查](#重启与外部入口)选择发布与目标再启动。systemd 实例按[普通服务器部署指南](../deploy/linux-server.md#systemd)重装指向新版的 unit，再用 `sudo systemctl start fertile.target`。
5. **复验后恢复接入。** 核对守护器报告的发布、服务 PID、实际数据根和库、HTTP 就绪、旧回测结果以及 worker 已就绪并使用原持久队列。按[WebSocket 入口核对](#websocket-入口核对)复验市场流。确认单一采集器接手、订阅恢复且水位推进，记录停机区间的缺口或未知状态。核对 notifier 与计划启用状态一致，再恢复提交与调度。
6. **失败时保持维护状态。** 含新增迁移的发布按[从升级前快照回滚并切回新版](#从升级前快照回滚并切回新版)执行。将迁移前备份恢复到新建的 `data-paper-rollback-<sha>` 和 `web_rollback_<sha>`，再在这对目标上运行旧发布，保留新版原数据与迁移台账。旧代码不得连接新版写过的数据库。原目标上的回退只在无新增迁移、两版数据和配置兼容、且旧发布所连接的数据库未被新版写过时适用。其余情况保持业务停止，按隔离恢复流程核对可用快照与恢复目标。

### 整栈维护

`status`、`logs` 和 `restart` 按当前持久根选择已安装且可连接的 systemd 管理器，其他实例沿用 supervisord。下面是云盒子的诊断与停机命令。凡需启动或重启服务，先按[重启检查](#重启与外部入口)确认发布和数据目标。

```sh
scripts/fertile status --json
scripts/fertile logs finance-api
scripts/fertile stop web
scripts/fertile down
```

普通 `rollback` 只适用于无新增迁移、两版数据和配置兼容、且旧发布所连接的数据库未被新版写过的场景。若私有 supervisor 仍管理正在运行的共享服务，先由 Ruler 处理所有权。部署记录包括两版提交、三个 gitlink、数据根、库名、快照路径、迁移状态和操作时间。

### 从升级前快照回滚并切回新版

含新增迁移的发布，先将迁移前备份恢复到新建的 `data-paper-rollback-<sha>` 数据根和 `web_rollback_<sha>` 数据库，再在其上运行旧发布。`<sha>` 为升级前的完整 fertile 提交号。原 `data-paper`、现网库 `web` 及其迁移台账保留给新版。即使兼容性检查通过，旧代码也不得连接新版写过的数据库。

1. 暂停接单和 notifier，排空作业，在部署记录中保存新版完整 fertile SHA、版本指针、数据根、研究根、数据库、PG 端口和业务水位。保全新版快照与配套密文，把已核实的迁移前快照留在轮转目录外。快照必须与旧制品、行情来源和迁移起点相符，并包含 `postgres.dump`。缺少匹配快照时停止回退。先停用直接调用 `up`、`ensure`、`ensure-up` 的巡检、自启与外部调度，并暂停备份发布和通知。停下业务和 supervisor，确认原采集器释放写锁和 IBKR 订阅。受管库此时单独启动本机 PostgreSQL。若存在逐服务发布指针，停栈后按[热升级手册](../deploy/hot-upgrade.md#升级与兼容回退)私下归档，回退期间保持整栈使用同一旧发布。
2. 从支持外部回退校验的控制器源码检出执行下例。先在 Bash 会话设置 `FERTILE_ROOT`、本机端口 `FERTILE_POSTGRES_PORT`、旧提交 `old_sha` 和迁移前快照的绝对路径 `pre_upgrade`，并准备已安装的工具链。含 F3 封段的快照需提供能读取该归档的 `FERTILE_BACKUP_WAL_BIN`。库名与目录必须尚未使用。`--dry-run` 校验快照、归档和目标占用。任何失败都应停止，按[恢复步骤](backup.md#恢复步骤)处理，保留已有目标供核对。`runtime.env` 和 `env.sh` 保持原样，快照里的配置只供核对。

   外部 PostgreSQL 的 supervisord 实例，把 `rollback_db_mode` 设为 `external`，由 DBA 提供隔离目标所在服务器的 `PGHOST`、`PGPORT`、`PGUSER` 和 libpq 管理认证，以同一快照恢复到 `web_rollback_<sha>`。同时在 `secrets/web-rollback-database.env` 配置仅能访问恢复库的应用身份。该文件是运行账户所有的 0600 常规文件，只包含 `DATABASE_URL` 赋值。连接 URI 的路径须为 `/web_rollback_<sha>`，`dbname` 查询参数会被拒绝。保留正常运行的 `secrets/web-database.env` 供切回新版。受管库使用默认的 `managed` 模式。

   ```bash
   (
     set -euo pipefail
     export FERTILE_ROOT="${FERTILE_ROOT:?请设置实例的持久根}"
     export FERTILE_POSTGRES_PORT="${FERTILE_POSTGRES_PORT:?请设置实际 PostgreSQL 端口}"
     [[ "$FERTILE_ROOT" == /* && "${pre_upgrade:?请设置迁移前快照}" == /* ]]
     [[ "${old_sha:?请设置旧发布提交}" =~ ^[0-9a-f]{40}$ ]]
     test -s "$pre_upgrade/postgres.dump"
     rollback_record="$FERTILE_ROOT/migration-rollback.sha"
     test ! -e "$rollback_record" && test ! -L "$rollback_record" || exit 1
     rollback_db_mode=${rollback_db_mode:-managed}
     old_data="$FERTILE_ROOT/data-paper-rollback-$old_sha"
     old_db="web_rollback_$old_sha"
     test ! -e "$old_data" && test ! -L "$old_data" || exit 1
     pg=(env -u PGHOSTADDR -u PGSERVICE)
     case "$rollback_db_mode" in
       managed) pg+=("PGHOST=$FERTILE_ROOT/run/postgres" "PGPORT=$FERTILE_POSTGRES_PORT" "PGUSER=$(id -un)") ;;
       external) pg+=("PGHOST=${PGHOST:?请设置隔离库服务器}" "PGPORT=${PGPORT:?请设置外部端口}"
                     "PGUSER=${PGUSER:?请设置恢复角色}") ;;
       *) exit 1 ;;
     esac
     occupied=$("${pg[@]}" psql -XAt -v ON_ERROR_STOP=1 -d postgres \
       -c "SELECT 1 FROM pg_database WHERE datname = '$old_db'")
     test -z "$occupied"
     restore_target=(--target-data-root "$old_data" --target-database "$old_db")
     "${pg[@]}" scripts/restore.sh "$pre_upgrade" --dry-run "${restore_target[@]}"
     "${pg[@]}" scripts/restore.sh "$pre_upgrade" "${restore_target[@]}"

     old_env=(env -u FERTILE_SERVICE_MANIFEST -u FERTILE_ROLLBACK_EXTERNAL_DB "FERTILE_DATA_ROOT=$old_data"
              "FERTILE_RESEARCH_ROOT=$old_data/research" "FERTILE_WEB_DATABASE=$old_db"
              FERTILE_ENABLE_WEB_NOTIFIER=0 FERTILE_BACKUP_REMOTE=)
     rollback_target=("$old_sha")
     if [[ "$rollback_db_mode" == external ]]; then
       old_env+=(FERTILE_ROLLBACK_EXTERNAL_DB=1)
       rollback_target+=(external)
     else
       scripts/postgres.sh stop
     fi
     (umask 077; set -C; printf '%s\n' "${rollback_target[@]}" > "$rollback_record")
     "${old_env[@]}" scripts/fertile rollback --switch-only "$old_sha"
     "${old_env[@]}" scripts/fertile ensure-up
     "${old_env[@]}" scripts/fertile data-root
     "${old_env[@]}" scripts/fertile status --json
   )
   ```

3. 核对 `current`、实际 web 连接的库名、SQLx 台账、finance 数据根与研究根，再确认 web 和 finance API 就绪并读到升级前已有的作业。核对行情来源和缺口，保持 IBKR paper 只读与单一采集订阅。只有在上述核对通过后才恢复接单。`migration-rollback.sha` 首行记录已恢复的旧发布 SHA，外部库另有一行 `external`，先写入持久盘再切换 `current`。回退期间的手动启动、重启及巡检恢复一律使用[重启检查](#重启与外部入口)从记录重建隔离参数，继续停用裸启动入口。重新启用 notifier 或异地备份前，先核对回退窗口的状态，避免重复投递或把隔离分支发布为原备份链的后继。外部库需另按 [备份操作](backup.md#配置) 为隔离库安排 libpq 备份连接。
4. 切回新版时，暂停接单、排空旧版作业并停止隔离栈。需要保留窗口内的写入时，将隔离数据根和旧库另作快照，记录待对账的作业、事件、邮件和行情缺口。保持业务停止，先选回并核对部署记录中的新版制品及原数据根、研究根和库，再将 `migration-rollback.sha` 移入本次私有归档。按[重启检查](#重启与外部入口)启动新版，核对迁移台账、就绪和已有作业后恢复业务及普通调度。两套数据分别留存，回退窗口的写入经业务对账后再决定如何补录。

systemd 使用独立实例的启动配置和恢复目标。连接外部库时，在隔离实例的常规 web 凭据文件中配置只指向 `web_rollback_<sha>` 的 `DATABASE_URL`，`FERTILE_WEB_DATABASE` 使用默认值。按[普通服务器部署指南](../deploy/linux-server.md#systemd)安装指向旧版的 unit，启动前核对卷和数据库连接。原实例的配置文件保留供切回新版。

## 重启与外部入口

主机重启、守护器重建或巡检需启动服务时，先按 [bootstrap](../deploy/bootstrap.md) 恢复工具链，再从支持外部回退校验的控制器源码检出执行下例。使用部署记录中规范化的绝对 `FERTILE_ROOT` 和本机实际 `FERTILE_POSTGRES_PORT`。`normal_sha` 是部署记录中核准使用现网目标的新版完整 fertile SHA，须独立于 `current` 取值。回退期间以 `migration-rollback.sha` 中的 SHA 和外部库标记选回隔离目标。外部库由控制器和进程启动器核对专用凭据指向恢复库，凭据缺失或指向其他库时拒绝启动。记录缺失且 `current` 不匹配新版，或回退记录无效、对应发布或目录不符时，保持停机并核对部署记录。所有常规启动和自启调用都须先通过这些检查。

```bash
(
  set -euo pipefail
  export FERTILE_ROOT="${FERTILE_ROOT:?请设置实例的持久根}"
  export FERTILE_POSTGRES_PORT="${FERTILE_POSTGRES_PORT:?请设置实际 PostgreSQL 端口}"
  [[ "$FERTILE_ROOT" == /* && -L "$FERTILE_ROOT/current" ]]
  rollback_record="$FERTILE_ROOT/migration-rollback.sha"
  launch_env=(env -u FERTILE_SERVICE_MANIFEST -u FERTILE_ROLLBACK_EXTERNAL_DB)
  if [[ -e "$rollback_record" || -L "$rollback_record" ]]; then
    [[ -f "$rollback_record" && ! -L "$rollback_record" ]]
    mapfile -t rollback_target < "$rollback_record"
    launch_sha=${rollback_target[0]:-}
    [[ "$launch_sha" =~ ^[0-9a-f]{40}$ ]]
    old_data="$FERTILE_ROOT/data-paper-rollback-$launch_sha"
    [[ -d "$old_data" && ! -L "$old_data" ]]
    launch_env+=("FERTILE_DATA_ROOT=$old_data" "FERTILE_RESEARCH_ROOT=$old_data/research"
                 "FERTILE_WEB_DATABASE=web_rollback_$launch_sha"
                 FERTILE_ENABLE_WEB_NOTIFIER=0 FERTILE_BACKUP_REMOTE=)
    if (( ${#rollback_target[@]} == 2 )) && [[ "${rollback_target[1]}" == external ]]; then
      launch_env+=(FERTILE_ROLLBACK_EXTERNAL_DB=1)
    elif (( ${#rollback_target[@]} != 1 )); then
      exit 1
    fi
  else
    launch_sha="${normal_sha:?请从部署记录设置核准的新版提交}"
    [[ "$launch_sha" =~ ^[0-9a-f]{40}$ ]]
  fi
  [[ "$(readlink -e -- "$FERTILE_ROOT/current")" == "$FERTILE_ROOT/releases/$launch_sha" ]]
  "${launch_env[@]}" scripts/fertile ensure-up
  "${launch_env[@]}" scripts/fertile status --json
)
```

启动后核对实际数据库、finance 数据根、研究根和行情水位。回退期间按[隔离恢复流程](#从升级前快照回滚并切回新版)核对旧版记录。常规运行时再用 `tests/e2e-smoke.sh local --paper-only` 验收本机 web。[健康检查](health-checks.md)

`deploy/service.yaml` 的 `infrastructure.tunnel.public_origin` 默认为 `null`。本机保持公网巡检未配置。未来生产服务器由运维显式配置自己的 HTTPS origin，设置 `FERTILE_PUBLIC_ORIGIN` 或巡检覆盖文件后，healthwatch 才会从该服务器独立探测公网 `/healthz/ready`。[巡检配置](monitoring.md#运行与配置)

## 管理令牌

web 使用 `$FERTILE_ROOT/secrets/web.env` 中唯一的 `WEB_ADMIN_TOKEN` 赋值，文件须由运行账户持有且权限为 0600。已有令牌由管理员管理。新实例可用下面的命令生成首次令牌，遇到已有文件会退出。提交回测等写操作需要 `Authorization: Bearer <token>`，只读看板和公开查询无需令牌。

~~~sh
umask 077
mkdir -p "$FERTILE_ROOT/secrets"
python3 - <<'PYTHON'
import os
from pathlib import Path
import secrets

path = Path(os.environ["FERTILE_ROOT"], "secrets", "web.env")
with path.open("x") as output:
    output.write("WEB_ADMIN_TOKEN=" + secrets.token_hex(32) + "\n")
PYTHON
scripts/fertile restart web
~~~

## 演练前的记录

保存 scripts/fertile status --json 输出、UTC 时间、当前提交和最后一个已验证备份。finance-api 与 web 的 PID 用于确认 PostgreSQL 恢复期间它们是否重启。操作先在隔离实例或预定的维护窗口进行。模拟重启会终止当前受管服务树。

~~~sh
git rev-parse HEAD
git submodule status --recursive
scripts/fertile status --json
tests/chaos-restart.sh
tests/simulate-reboot.sh
~~~

## 备份、恢复与账户运维

执行 `snapshot=$(scripts/backup.sh)` 保存输出的本机快照路径。备份成功后，执行 `scripts/restore.sh "$snapshot" --dry-run` 检查快照，可在 PostgreSQL 停止时执行。要检查目标占用，带上 `--target-data-root` 与 `--target-database` 并保持 PostgreSQL 可用。正式恢复使用同一组目标，且须先停止所有写入方。默认保留 7 个每日和 4 个每周快照。[备份操作](backup.md)

整机或持久盘丢失时，在独立运行根取得离线恢复材料并装配相同的 `crypt` 配置。执行 `python3 scripts/backup_remote.py status` 查询最后可恢复到的时间与点 ID，再选择一个独立日全量或具有完整父链的小时点，恢复到空数据目录和隔离库。超过一小时的告警优先检查远端容量、密钥访问权限和父链完整性。由 vira 确定私人 Drive 账户、可用容量与离线密钥保管方式后，再启用异地备份。[异地恢复与演练](backup.md#从远端恢复到指定水位)

在运行时根检查 logs/alerts/alerts.jsonl 和 logs/healthwatch/samples.jsonl，把服务停用、恢复、公网探测和备份年龄分开阅读。通知默认写入本地，维护人员可按 [监控](monitoring.md) 配置独立 webhook。paper Gateway 的安装、登录失败预算、2FA、每日自动重启和健康检查见 [Gateway 运维](ibgateway.md)。不要在运行记录中复制凭据、完整账号或本机令牌。

## 整栈冒烟与故障验收

先生成示例回测，再用持久目录的 web 管理令牌运行本机冒烟。脚本检查系统分项、市场数据年龄、首页 JS/CSS，并实际提交一个幂等回测，直到取得带精确数值的成功摘要。

本机冒烟仍会记录关闭的可选进程。就绪检查跳过状态为 `disabled` 且 PID 为 0 的可选进程，例如默认关闭的 projector 和 notifier。必需进程和已启用的可选进程须通过各自的进程与 HTTP 检查。

首页必须至少声明一个同源 JavaScript 模块和一个同源样式表，所有同源模块和样式表都要通过 HTTP 状态、媒体类型和响应内容检查。同源比较中，HTTP 的 `:80` 和 HTTPS 的 `:443` 各自与省略端口等价。脚本跳过所有跨域 `<script src>` 和样式表，包括 Cloudflare Insights 注入的脚本，并写入 `event` 为 `skipped cross-origin asset` 的 JSON 日志，包含资源类型 `kind`（`js` 或 `css`）和主机名 `host`。

本机冒烟还按[许可证 HTTP 路径](#第三方许可证复验)验证两个第三方路径均返回 200 和预期许可证文本，根路径 `/NOTICE` 返回 200 且正文与第三方 NOTICE 响应逐字节一致，误加 `/static` 前缀的两个路径均返回 404。

本机部署复验使用 `--paper-only`。该选项跳过本机 healthwatch、xvfb 和 ib-gateway 的就绪要求，仍要求其余核心进程运行、collector live 为 200，并额外接受 collector ready 为 503。本机与公网冒烟在 `--paper-only` 下都接受 `system/status` 与 `market/status` 一致的 IB `recovering` 状态，此时暂免行情新鲜度检查。Web 就绪、数据库、finance API、前端资源和回测提交仍需通过。公网与本机请求都发送 `fertile-e2e-smoke/<VERSION>` User-Agent。

~~~sh
tests/e2e-smoke.sh local --paper-only
~~~

未来生产服务器的运维人员配置自己的 HTTPS 入口并确认它路由到该服务器的 web 后，可在维护窗口显式运行公网冒烟。将下例中的占位 origin 替换为实际入口，未接入 IB 行情的 paper 部署另加 `--paper-only`。

~~~sh
tests/e2e-smoke.sh public 'https://<your-origin>'
~~~

在维护窗口逐项执行，确保每个场景结束后恢复业务再进入下一项。进程测试只向已核对父进程和 PID 身份的本实例服务发信号。停库场景同时检查 web 和 finance PID。

~~~sh
python3 tests/chaos/services.py services
python3 tests/chaos/services.py postgres
python3 tests/chaos/services.py reboot
~~~

网络演练依赖已登录的只读 paper Gateway 和稳定的 1 分钟行情。它只给本机 paper 连接施加限定故障，默认只检查本地端点。未来生产服务器如需同时观察公网入口，由运维显式传入 `--public-origin`。[网络演练](../tests/chaos/network.md)

## 24 小时观察

在本机上述测试通过后，保留 healthwatch 的持续采样，尽量覆盖一个美股正常交易时段。先核实采样显示的发布提交、数据源和公网探针的跳过状态。将起点设为一条真实样本的 UTC 时间，等待至少一天，最后一条样本必须覆盖所选终点。

~~~sh
root=${FERTILE_ROOT:?先设置实例的持久根}
start=$(tail -1 "$root/logs/healthwatch/samples.jsonl" | python3 -c 'import json,sys; print(json.load(sys.stdin)["timestamp"])')
printf '%s\n' "$start" > "$root/run/soak-start.utc"
# 至少 24 小时后，在包含上述起点的同一部署上执行
start=$(cat "$root/run/soak-start.utc")
python3 scripts/soak_report.py \
  --samples "$root/logs/healthwatch/samples.jsonl" --alerts "$root/logs/alerts/alerts.jsonl" \
  --start "$start" --require-hours 24 --label "本地压力测试机 · IBKR paper" \
  --output "ops/reports/${start%%T*}-soak.md"
~~~

采样不足或存在缺口时生成器会写明覆盖区间并返回非零。核对真实服务可用率、断线及恢复时间、行情最大陈旧时间和跳过的探针；外部原因与 2FA 人工步骤附入同一条验收记录。隔离 ReplaySource 或协议替身的报告应使用其实际环境标签。
