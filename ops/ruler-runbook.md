# Maas · Ruler 云盒子操作手册

常规发布依据已验证的 fertile tag。当前高可用联调使用指定的 fertile 集成提交，其 gitlink 锁定 finance、web 和 frontend。所有命令从总仓库根目录运行。保留上一次可用的构建和备份，再切换代码或数据格式。

Ruler 负责目标盒子部署、现有网站、Cloudflare 隧道、DNS 和线上恢复。现网站使用 127.0.0.1:8080。fertile 的独立源站为 127.0.0.1:8090，目标域名为 https://fertile.viramoon.com。fertile 的脚本只管理本项目进程和持久目录。部署不安装开机钩子、shell 启动项或主机定时任务。

## 首次启动

1. 用 box 用户将源码 checkout 到 /home/box/fertile-source。初始化三个私有子模块及 Nautilus 源码。核对目标提交和 profile。
2. 按 [bootstrap](../deploy/bootstrap.md) 安装锁定的工具，执行 source /home/box/fertile/env.sh。确认磁盘剩余足以构建 Rust、Python wheel、前端与备份。
3. 按 web 手册确认静态目录、数据库角色和管理令牌。凭据文件只允许属主读写。[web RUNBOOK](https://github.com/viramoon/web/blob/main/RUNBOOK.md)
4. 在本机 Gateway 完成 paper 登录前，公网可达性、paper 账号和行情连续性独立记录。开发演练可显式选 replay，并为回放与真实行情分别安排持久数据根。

```sh
git submodule update --init --recursive
candidate=$(git rev-parse HEAD)
scripts/fertile build --ref "$candidate"
scripts/fertile ensure-up
scripts/fertile status --json
curl -fsS http://127.0.0.1:8090/api/v1/system/status
```

当前版本目录的 bin/ 下放编译产物，static/ 存放同一批次的 Vite dist。finance 与 web 各自读取版本目录内的配置，PostgreSQL、队列和 catalog 保留于持久数据目录。首次准备示例摘要时，加载同一个环境与数据根，再从当前 release 中执行以下命令。

`scripts/fertile data-root` 使用与 supervisor 相同的解析规则。已导出的 `FERTILE_DATA_ROOT` 优先，其次读取 `$FERTILE_ROOT/config/runtime.env`。例如在文件中填写 `FERTILE_DATA_ROOT=/home/box/fertile/data-paper` 即可使用独立的 paper 数据根。两处都未设置时，IB 使用 `$FERTILE_ROOT/data`，replay 使用 `$FERTILE_ROOT/data/replay`。

~~~sh
source "${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}/env.sh"
(
  FERTILE_DATA_ROOT=$(scripts/fertile data-root) || exit
  export FERTILE_DATA_ROOT FINANCE_BUILD_ROOT="$FERTILE_ROOT/current/runtime"
  cd "$FERTILE_ROOT/current/finance" && PYTHONDONTWRITEBYTECODE=1 bash scripts/backtest-sample.sh
)
curl -fsS http://127.0.0.1:8090/api/v1/backtests/sample
~~~

## finance 7600be4 升级

| 再部署项目 | 操作 |
| --- | --- |
| 新增服务环境变量 | 无。继续使用现有 `runtime.env` 和整栈工具链。 |
| 新增必需数据 | 无。保留现有实时 catalog、回测队列和 `FERTILE_RESEARCH_ROOT` 下经审核的日线快照。 |
| 必需数据迁移 | 无。finance 队列 schema 和 web SQLx 迁移文件未变。保留升级前的备份，再按集成版本部署 finance、web 和前端。 |

部署后提交一个新的历史日线作业并核对成交审计。finance 与 web 的结果依序包含 `50y`、`10y`、`5y`、`longest_sample` 四个窗口，最长样本的 `requested_years` 为 `null`。有值的窗口绩效字段使用十进制字符串。在 `/backtests` 确认新作业显示「最长样本」，覆盖不足或缺少指标的窗口将净值变动显示为「不可用」。再打开一条已有历史作业，确认仍能查看固定窗口。

旧 finance API 的字段类型无法读取新版写入的四窗口摘要。回滚到升级前版本时，暂停接单并保全升级后的队列，再为旧版选用兼容的独立数据根或恢复升级前的备份。

如需使用公开历史研究数据，为人工命令设置原始文件目录 `FINANCE_HISTORY_ARCHIVE` 与 Yahoo 观测截止日 `FINANCE_YAHOO_AS_OF`，按手册保存经许可的 Yahoo、Ken French、FRED 原始文件，记录获取日期、来源和实际覆盖范围。给归档命令单独设置 `FERTILE_DATA_ROOT`，使用与在线采集及官方回测隔离的持久目录。公开历史版本保存在该数据根的 `catalog/historical`。fertile 的采集 catalog 备份会拒绝该归档中的 `current` 符号链接，因此请暂停归档写入，独立备份原始文件、`current` 和它指向的版本目录。[finance 手工归档步骤](https://github.com/viramoon/finance/blob/7600be468541d381cb9014ae7d01237f499c0db5/RUNBOOK.md#手工保存公开历史数据)

有旧 SPY 固定 21:00 UTC 日线且要切换到新归档时，先用 XNYS 会话表核对标准时、夏令时和半日市，备份旧 catalog，并记录来源、日期区间和价格口径。再在独立数据根用经许可的源 OHLCV、调整收盘价和公司行为重新导入。核对覆盖区间、条数、会话收盘时间、源价精度、复权因子和公司行为之后，只切换研究读取路径，并保留旧数据供复现与回退。[finance SPY 迁移说明](https://github.com/viramoon/finance/blob/7600be468541d381cb9014ae7d01237f499c0db5/catalog/README.md#既有-spy-固定-2100-utc-数据)

## 导入历史日线

选择经许可的原始 OHLCV，确认标的是 NYSE 上市的 USD 股票或 ETF，并核对区间内无拆股。价格使用美分、成交量使用整股，UTC 收盘时间由日历生成。生产不随包附带行情。将批准的 CSV 从独立路径导入，持久配置的 FERTILE_RESEARCH_ROOT 可选择 fertile 的 data/research 下的具体目录。默认命令如下。[finance 历史回测手册](https://github.com/viramoon/finance/blob/main/docs/historical-backtest.md)

~~~sh
export FERTILE_RESEARCH_ROOT="$FERTILE_ROOT/data/research"
PYTHONPATH="$FERTILE_ROOT/current/finance/python" \
  "$FERTILE_ROOT/current/runtime/venv/bin/python" -m fertile_finance.research_catalog \
  /path/to/approved.csv --instrument IBM.XNYS --asset-type stock \
  --kind historical --source "Approved provider IBM" --raw-split-free-reviewed
curl -fsS http://127.0.0.1:8090/api/v1/backtests/datasets
tests/e2e-smoke.sh local --research-instrument IBM.XNYS --research-kind historical
~~~

打开 /backtests 填写均线天数与目标股数，临时输入 web 提交令牌。结果应显示作业状态、来源、可用窗口和成交审计。提交超时先查询原幂等键，收到 404 后才用同一键和参数重试。样本缺口按失败处理，数据更新和回滚按 catalog 快照进行。

## 启停、诊断与回滚

```sh
scripts/fertile status --json
scripts/fertile logs finance-api
scripts/fertile stop web
scripts/fertile restart web
scripts/fertile down
scripts/fertile ensure-up
scripts/fertile rollback <previous-fertile-commit>
```

普通 `rollback` 适用于两版共享兼容的数据和配置。传入已构建的 fertile 提交，控制器验证制品后切换服务与静态资源。旧版无法读取新版队列或摘要时，按下方步骤切换独立数据。若私有 supervisor 仍管理正在运行的共享服务，先由 Ruler 处理所有权。部署记录包括两版提交、三个 gitlink、数据根、库名、快照路径、迁移状态和操作时间。

### 从升级前快照回滚并切回新版

使用包含本节脚本的源码检出执行整套步骤。示例针对本机受管 PostgreSQL、`data-paper` 和已经构建的 `7f88ba4`，工作路径以现场记录为准。若 web 使用 `secrets/web-database.env` 中的外部 `DATABASE_URL`，应由数据库管理员先安排独立的连接配置和切换步骤。本节的 `FERTILE_WEB_DATABASE` 适用于本机连接。

1. 暂停接单，等运行中的作业结束，记录新版和升级前快照。载入工具环境，确认当前数据根、库名和 PostgreSQL 端口。先把旧快照保存在自动轮转目录外，避免保全新版时触发清理。以下各段在同一个 Bash 会话逐段执行，每条命令成功后再继续。

   ```bash
   source /home/box/fertile/env.sh
   new_sha=$(basename -- "$(readlink -f -- "$FERTILE_ROOT/current")")
   new_data=$(scripts/fertile data-root)
   new_db=web  # 现场若配置了 FERTILE_WEB_DATABASE，填对应库名
   export FERTILE_POSTGRES_PORT=5432  # 现场若配置了其他端口，填对应端口
   old_sha=7f88ba4
   old_data="$FERTILE_ROOT/data-paper-rollback-$old_sha"
   old_db="web_rollback_$old_sha"
   archive=$(mktemp -d "$FERTILE_ROOT/rollback-archives.XXXXXXXX")
   cp -a -- "$FERTILE_ROOT/backups/20261125T180713.484833Z" "$archive/before-upgrade"
   pre_upgrade="$archive/before-upgrade"
   scripts/fertile status --json
   ```

2. 用 supervisor 停业务。`down` 会一并停 PostgreSQL，随后单独启动 PostgreSQL，整个备份和恢复期间保持它可用。备份新版数据和队列，并把快照保存到轮转目录外。确认 `snapshot.json` 记录的库名和来源符合切换计划，升级前快照应有 `postgres.dump`。

   ```bash
   scripts/fertile down
   scripts/postgres.sh start
   new_snapshot=$(FERTILE_DATA_ROOT="$new_data" PGDATABASE="$new_db" scripts/backup.sh)
   cp -a -- "$new_snapshot" "$archive/new-version"
   ```

3. 验证升级前快照再恢复到新目录和新库。首次恢复使用空目录或尚未创建的目录，以及空库或不存在的库。

   ```bash
   scripts/restore.sh "$pre_upgrade" --dry-run --target-data-root "$old_data" --target-database "$old_db"
   (cd "$pre_upgrade" && sha256sum --check SHA256SUMS)
   scripts/restore.sh "$pre_upgrade" --target-data-root "$old_data" --target-database "$old_db"
   scripts/postgres.sh stop
   ```

4. 用同一份新版控制器验证并切换 `current`，再由 supervisor 启动旧版。这里通过命令环境覆盖 `$FERTILE_ROOT/config/runtime.env` 中的三条运行指针。保留该文件、新版数据根和库名，也保留 `env.sh` 的工具链配置。快照的 `config/runtime.env` 可供核对，勿复制到持久配置目录。回滚期间手工执行 `ensure-up` 或主机重启后重新拉起 supervisor 时，沿用下列 `old_env`。

   ```bash
   old_env=(env "FERTILE_DATA_ROOT=$old_data" "FERTILE_RESEARCH_ROOT=$old_data/research" "FERTILE_WEB_DATABASE=$old_db")
   "${old_env[@]}" scripts/fertile rollback --switch-only "$old_sha"
   "${old_env[@]}" scripts/fertile ensure-up
   ```

5. 核对 `current`、数据根、旧库连接和服务状态。确认 web、finance API 就绪，查询一条升级前已有的作业，再运行 `--paper-only` 冒烟。冒烟会在当前队列提交作业，需在核对指针后执行。公网入口验收按维护窗口要求执行，通过后再恢复接单。

   ```bash
   readlink -f -- "$FERTILE_ROOT/current"
   "${old_env[@]}" scripts/fertile data-root
   scripts/fertile status --json
   curl -fsS http://127.0.0.1:8090/api/v1/system/status
   psql -X -h "$FERTILE_ROOT/run/postgres" -p "$FERTILE_POSTGRES_PORT" -d "$old_db" \
     -c "SELECT datname, count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='fertile_web' GROUP BY datname"
   "${old_env[@]}" tests/e2e-smoke.sh local --paper-only
   "${old_env[@]}" tests/e2e-smoke.sh public https://fertile.viramoon.com --paper-only
   ```

6. 切回新版时，暂停接单并让旧版作业结束。需要保留窗口内的新写入时，依照第 2 步停业务并单独启动 PG，执行 `"${old_env[@]}" PGDATABASE="$old_db" scripts/backup.sh`，记录快照路径后执行 `scripts/postgres.sh stop`。通过原先记录的新版指针启动新版，研究目录继续使用原配置。确认新版作业可查，再重复运行状态检查和 paper-only 冒烟。

   ```bash
   scripts/fertile down
   new_env=(env "FERTILE_DATA_ROOT=$new_data" "FERTILE_WEB_DATABASE=$new_db")
   "${new_env[@]}" scripts/fertile rollback --switch-only "$new_sha"
   "${new_env[@]}" scripts/fertile ensure-up
   scripts/fertile status --json
   "${new_env[@]}" tests/e2e-smoke.sh local --paper-only
   ```

## 重启与外部入口

主机重启后由 Ruler 按 bootstrap 核对持久根和工具链，运行 scripts/fertile ensure-up。在运行配置一致时，重复调用可保留现有服务 PID。Ruler 决定调用时机和外部调度。优先核对持久数据库、finance 数据路径和行情新鲜度，再分别验收本地 web 与公网。记录端点和业务状态。[健康检查](health-checks.md)

Ruler 在现有 Tunnel 和 DNS 中为 fertile.viramoon.com 配置 http://127.0.0.1:8090 的路由。现有站点继续使用 8080，现场核验的进程、启动方式、隧道配置和凭据由 Ruler 管理。deploy/service.yaml 只记录公网 URL 和本机端口。healthwatch 独立探测 /healthz/ready 并记录失败，隧道修复由 Ruler 执行。

## 管理令牌

web 使用持久目录 secrets/web.env 中唯一的 WEB_ADMIN_TOKEN 赋值，文件权限为 0600。已有令牌由管理员管理；新盒子可用下面的命令在本地生成首次令牌，遇到已有文件会直接退出。令牌用于经 web 提交回测，前端和公开只读查询均无需持有它。

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

执行 `snapshot=$(scripts/backup.sh)` 保存输出的快照路径。备份成功后，执行 `scripts/restore.sh "$snapshot" --dry-run` 检查快照。实际写入时使用 `--target-data-root` 和 `--target-database` 指定目标。数据库须在所有写入方退出后保持可用。默认保留 7 个每日和 4 个每周快照，整机恢复还需异机副本。[备份操作](backup.md)

在运行时根检查 logs/alerts/alerts.jsonl 和 logs/healthwatch/samples.jsonl，把服务停用、恢复、公网探测和备份年龄分开阅读。通知默认写入本地，维护人员可按 [监控](monitoring.md) 配置独立 webhook。paper Gateway 的安装、登录失败预算、2FA、每日自动重启和健康检查见 [Gateway 运维](ibgateway.md)。不要在运行记录中复制凭据、完整账号或本机令牌。

## 整栈冒烟与故障验收

先生成示例回测，再用持久目录的 web 管理令牌运行冒烟。脚本检查系统分项、市场数据年龄、首页 JS/CSS，并实际提交一个幂等回测，直到取得带精确数值的成功摘要。公开入口只填写用户已确认、由其控制且确实路由到本机 web 的 HTTPS origin。

首页必须至少声明一个同源 JavaScript 模块和一个同源样式表，所有同源模块和样式表都要通过 HTTP 状态、媒体类型和响应内容检查。同源比较中，HTTP 的 `:80` 和 HTTPS 的 `:443` 各自与省略端口等价。脚本跳过所有跨域 `<script src>` 和样式表，包括 Cloudflare Insights 注入的脚本，并写入 `event` 为 `skipped cross-origin asset` 的 JSON 日志，包含资源类型 `kind`（`js` 或 `css`）和主机名 `host`。

~~~sh
tests/e2e-smoke.sh local
tests/e2e-smoke.sh public https://fertile.viramoon.com
~~~

只验收未接入 IB 行情的 paper 部署时，为本机或公网冒烟显式添加 `--paper-only`。本机跳过 healthwatch、xvfb 和 ib-gateway 的就绪要求，仍要求其余核心进程运行、collector live 为 200，并额外接受 collector ready 为 503。两个入口都接受 `system/status` 与 `market/status` 一致的 IB `recovering` 状态，此时暂免行情新鲜度检查。Web 就绪、数据库、finance API、前端资源和回测提交仍需通过。公网与本机请求都发送 `fertile-e2e-smoke/<VERSION>` User-Agent。

~~~sh
tests/e2e-smoke.sh local --paper-only
tests/e2e-smoke.sh public https://fertile.viramoon.com --paper-only
~~~

在维护窗口逐项执行，确保每个场景结束后恢复业务再进入下一项。进程测试只向已核对父进程和 PID 身份的本实例服务发信号。停库场景同时检查 web 和 finance PID。

~~~sh
python3 tests/chaos/services.py services
python3 tests/chaos/services.py postgres
python3 tests/chaos/services.py reboot
~~~

网络演练依赖已登录的只读 paper Gateway 和稳定的 1 分钟行情。它只给本机 paper 连接施加限定故障，公网只观察。真实 Tunnel 恢复的命令和验收由 Ruler 记录。[网络演练](../tests/chaos/network.md)

## 24 小时观察

在真实盒子上述测试通过后，保留 healthwatch 的持续采样，尽量覆盖一个美股正常交易时段。先核实当时采样显示的发布提交、数据源和公网入口。将起点设为一条真实样本的 UTC 时间，等待至少一天，最后一条样本必须覆盖所选终点。

~~~sh
root=/home/box/fertile
start=$(tail -1 "$root/logs/healthwatch/samples.jsonl" | python3 -c 'import json,sys; print(json.load(sys.stdin)["timestamp"])')
printf '%s\n' "$start" > "$root/run/soak-start.utc"
# 至少 24 小时后，在包含上述起点的同一部署上执行
start=$(cat "$root/run/soak-start.utc")
python3 scripts/soak_report.py \
  --samples "$root/logs/healthwatch/samples.jsonl" --alerts "$root/logs/alerts/alerts.jsonl" \
  --start "$start" --require-hours 24 --label "目标盒子 · IBKR paper 与已确认的 Tunnel" \
  --output "ops/reports/${start%%T*}-soak.md"
~~~

采样不足或存在缺口时生成器会写明覆盖区间并返回非零。核对真实服务可用率、断线及恢复时间、行情最大陈旧时间和跳过的探针；外部原因与 2FA 人工步骤附入同一条验收记录。隔离 ReplaySource 或协议替身的报告应使用其实际环境标签。
