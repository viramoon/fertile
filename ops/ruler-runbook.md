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
3. 受管 PostgreSQL 由 supervisord 的 `scripts/fertile ensure-up` 或 systemd 启动时的准备步骤以 `fertile_web` 运行新版 `web migrate`，成功后启动新版 web。使用外部 PostgreSQL 时，由数据库管理员安排显式迁移。以 web 运行账户加载服务所用的 `DATABASE_URL` 和 profile，设置 `WEB_CONFIG_DIR="$FERTILE_ROOT/current/web/config"`，执行新版 `current/bin/web migrate`，成功后再重启到新版 web。确认 SQLx 台账已记录 `20261126000000` 成功。
4. 核对新版 web PID、`/healthz/ready` 和 `/api/v1/system/status`，打开终端页面并核对同源 JS/CSS，按[第三方许可证复验](#第三方许可证复验)核对两个第三方路径和根路径 `/NOTICE` 的响应。在部署复验窗口运行 `tests/e2e-smoke.sh local --paper-only`，确认可提交回测。官方历史日线作业按[隔离历史日线验收](#部署后的隔离历史日线验收)使用隔离队列。
5. 降级到 W1 之前时，从升级前快照恢复独立的兼容数据库，保留已迁移的数据库和密文。旧版 SQLx 迁移器会拒绝台账中新增的迁移版本。使用新版控制器与兼容数据库切回旧制品，步骤见 [从升级前快照回滚并切回新版](#从升级前快照回滚并切回新版)。若重装旧版 systemd unit，先记录并撤下 `runtime.env` 中已配置的 W1 变量，切回新版后恢复。若还跨越 finance `7600be4`，为旧 finance 选择兼容的独立数据根或恢复首次写入四窗口摘要前的备份。

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

### 单进程热升级与回退

保留已通过隔离回读的旧版制品。从总仓检出暂存下一发布，确认前后两版可读取现有数据，按应用角色逐个切换。升级 finance API 或 web 时核对 collector 的 PID、水位和来源。切换采集器前准备同源封段、租约交接和缺口检查。使用 `hot-upgrade-status` 查询事务，遇到控制器意外退出或切换结果不确定时，先按实际进程、端口、水位和租约核对，再记录恢复结果。复核、命令和接口见[热升级与本机接口](../deploy/hot-upgrade.md)。

旧版回读无法通过时，使用隔离数据目录和数据库进行回退，保留新版数据用于恢复。整栈命令用于维护窗口。以下示例由获授权的部署账户在目标主机执行，开发与独立复测使用临时模板和合成进程。

### 整栈维护

`status`、`logs` 和 `restart` 按当前持久根选择已安装且可连接的 systemd 管理器，其他实例沿用 supervisord。下面的停机、恢复和回滚步骤用于云盒子。

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
   source "${FERTILE_ROOT:?先设置实例的持久根}/env.sh"
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

3. 验证升级前快照再恢复到新目录和新库。带显式目标的 `--dry-run` 会只读检查目标目录和库的占用情况。首次恢复使用空目录或尚未创建的目录，以及空库或不存在的库。遇到占用时，dry-run 会在标准错误中点名目录或库并退出 1。确认允许覆盖时可加 `--overwrite` 做预检，dry-run 会警告正式恢复将替换哪些内容，其余检查通过时退出 0。PostgreSQL 不可达或已存在的目标库无法读取时，命令会点名目标库并退出 1。即使看到 `SHA256 verified`，也要检查退出码再继续。

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

5. 核对 `current`、数据根、旧库连接和服务状态。确认 web、finance API 就绪，查询一条升级前已有的作业，再运行本机 `--paper-only` 冒烟。冒烟会在当前队列提交作业，需在核对指针后执行。通过本地复验后恢复接单。

   ```bash
   readlink -f -- "$FERTILE_ROOT/current"
   "${old_env[@]}" scripts/fertile data-root
   scripts/fertile status --json
   curl -fsS http://127.0.0.1:8090/api/v1/system/status
   psql -X -h "$FERTILE_ROOT/run/postgres" -p "$FERTILE_POSTGRES_PORT" -d "$old_db" \
     -c "SELECT datname, count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='fertile_web' GROUP BY datname"
   "${old_env[@]}" tests/e2e-smoke.sh local --paper-only
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

主机重启后由 Ruler 按 bootstrap 核对持久根和工具链，运行 `scripts/fertile ensure-up`。在运行配置一致时，重复调用可保留现有服务 PID。Ruler 决定调用时机和外部调度。核对持久数据库、finance 数据路径和行情新鲜度，再用 `tests/e2e-smoke.sh local --paper-only` 验收本机 web。记录端点和业务状态。[健康检查](health-checks.md)

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
