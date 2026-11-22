# Maas · Ruler 云盒子操作手册

常规发布依据已验证的 fertile tag。当前高可用联调使用指定的 fertile 集成提交，其 gitlink 锁定 finance、web 和 frontend。所有命令从总仓库根目录运行。保留上一次可用的构建和备份，再切换代码或数据格式。

## 首次启动

1. 用 box 用户将源码 checkout 到 /home/box/fertile-source。初始化三个私有子模块及 Nautilus 源码。核对目标提交和 profile。
2. 按 [bootstrap](../deploy/bootstrap.md) 安装锁定的工具，执行 source /home/box/fertile/env.sh。确认磁盘剩余足以构建 Rust、Python wheel、前端与备份。
3. 按 [web RUNBOOK](https://github.com/viramoon/web/blob/ha-runthrough/RUNBOOK.md) 配置 web 管理令牌。凭据文件只允许属主读写。保留 /home/box/.cloudflared/ 下的既有配置和凭据。
4. 在本机 Gateway 完成 paper 登录前，公网可达性、paper 账号和行情连续性独立记录。开发演练可显式选 replay，并为回放与真实行情分别安排持久数据根。

```sh
git submodule update --init --recursive
scripts/fertile build
scripts/fertile up
scripts/fertile status --json
curl -fsS http://127.0.0.1:8080/api/v1/system/status
```

当前版本目录的 bin/ 下放编译产物，static/ 存放同一批次的 Vite dist。finance 与 web 各自读取版本目录内的配置，PostgreSQL、队列和 catalog 保留于持久数据目录。首次准备示例摘要时，加载同一个环境与数据根，再从当前 release 中执行以下命令。

~~~sh
source /home/box/fertile/env.sh
# paper 默认 data；回放实例设置为 "$FERTILE_ROOT/data/replay"。
export FERTILE_DATA_ROOT="$FERTILE_ROOT/data" FINANCE_BUILD_ROOT="$FERTILE_ROOT/current/runtime"
(cd "$FERTILE_ROOT/current/finance" && PYTHONDONTWRITEBYTECODE=1 bash scripts/backtest-sample.sh)
curl -fsS http://127.0.0.1:8080/api/v1/backtests/sample
~~~

客户端提交回测必须保留幂等键，任何超时可用同一键重试并查询原作业。

## 启停、诊断与回滚

```sh
scripts/fertile status --json
scripts/fertile logs finance-api
scripts/fertile restart web
scripts/fertile down
scripts/fertile ensure
scripts/fertile rollback <previous-fertile-commit>
```

rollback 的参数为已经构建并验证的 fertile 提交。先核对迁移、队列和 catalog 格式，必要时先备份，再切换到匹配的服务与静态资源。部署记录包括当前/上次提交、三个 gitlink、迁移状态、操作开始和恢复时间。不要用单独替换前端或二进制的方式跨版本组合。

## 重启与外部入口

主机重启后先按 bootstrap 重建系统依赖，然后运行 scripts/fertile ensure。云盒子尚无已确认的持久启动钩子，Ruler 负责重启后的首次 ensure，之后巡检也可重复执行。优先检查守护、PostgreSQL、finance 数据路径与行情源，再检查 web 和 Tunnel。按 [健康检查](health-checks.md) 同时记录本地端点和业务状态。

cloudflared 使用既有 Tunnel，QUIC ingress 目标为 http://127.0.0.1:8080。公网域名候选为 www.viramoon.com 与 www.viramoonstar.com。由用户确认后再配置 public_origin 并对该 HTTPS 主机进行带鉴权的验收。密钥或 Cookie 不进入终端记录、告警或 PR。

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
