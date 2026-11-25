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

rollback 的参数为已经构建并验证的 fertile 提交。先核对迁移、队列和 catalog 格式，必要时先备份，再切换到匹配的服务与静态资源。旧版不能读取含历史作业的队列，即使作业已经结束。控制器会拒绝该组合。跨此边界回滚由 Ruler 先暂停接单、完成并备份当前队列，再选择旧版能读的独立持久数据根或受控恢复到升级前快照。记录当前和旧数据根，恢复制品时把它们与对应提交一起切换。使用本批控制器执行回滚以保留 8090 和外部隧道边界。若私有 supervisor 已管理正在运行的共享服务，操作会停在所有权检查，交由 Ruler 处理。部署记录包括当前/上次提交、三个 gitlink、迁移状态、操作开始和恢复时间。

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

执行 scripts/backup.sh 后记录其输出路径，先用 scripts/restore.sh --dry-run 检查所选快照。实际替换数据库前须让所有写入方退出，并让 PostgreSQL 保持可用。停机顺序、恢复前自动快照、中断后的重试和校验详见 [备份操作](backup.md)。默认保留 7 个每日和 4 个每周快照，仅保存在同一台盒子的备份无法应对整机丢失。

在运行时根检查 logs/alerts/alerts.jsonl 和 logs/healthwatch/samples.jsonl，把服务停用、恢复、公网探测和备份年龄分开阅读。通知默认写入本地，维护人员可按 [监控](monitoring.md) 配置独立 webhook。paper Gateway 的安装、登录失败预算、2FA、每日自动重启和健康检查见 [Gateway 运维](ibgateway.md)。不要在运行记录中复制凭据、完整账号或本机令牌。

## 整栈冒烟与故障验收

先生成示例回测，再用持久目录的 web 管理令牌运行冒烟。脚本检查系统分项、市场数据年龄、首页 JS/CSS，并实际提交一个幂等回测，直到取得带精确数值的成功摘要。公开入口只填写用户已确认、由其控制且确实路由到本机 web 的 HTTPS origin。

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
