# 普通 Linux 服务器部署

本指南适用于 Ubuntu 24.04 和 Debian 12 的 x86_64 主机。首次部署可用随构建提供的 replay 行情验证看板，再接入已授权的 IBKR paper 只读行情。部署需要能下载锁定的工具与构建依赖，以及足以容纳 Rust、Python wheel、前端构建和备份的持久磁盘。

## 1. 准备运行账户、源码与工具

使用一个普通 Linux 账户构建和运行服务。本例中的 `$USER` 和 `$(id -gn)` 分别是该账户和主组。拥有管理员权限的运维人员负责 apt、systemd 与反向代理操作。初始系统至少需要 Bash、Git、CA 证书和 curl。systemd 安装入口还需要发行版提供的 `python3`。

总仓库 `fertile` 及其子模块 `finance`、`web`、`frontend` 均为公开仓库。勿把令牌放入 clone URL。确定部署 commit 或 tag 后再初始化子模块，递归初始化也会取回 finance 的 Nautilus 源码。

```bash
git clone https://github.com/viramoon/fertile.git
cd fertile
# 生产发布时检出已批准的 fertile tag。集成部署可用获批的完整提交。
# git checkout --detach <approved-ref>
git submodule update --init --recursive
git submodule status --recursive

export FERTILE_PROFILE=stable
export FERTILE_ROOT=/var/lib/fertile
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" "$FERTILE_ROOT"
```

`stable` 默认使用 `/var/lib/fertile`。若要换挂载点，改用显式的 `FERTILE_ROOT`，或取消该变量并设置 `FERTILE_BOX_HOME`，此时使用 `$FERTILE_BOX_HOME/fertile`。以下步骤在同一个 Bash 会话中进行，均使用实际的持久根。新会话中先导出该根，再加载 `source "$FERTILE_ROOT/env.sh"`。源码目录与持久根分开。`releases/` 和 `current` 保存构建与当前指针，`data/`、`config/`、`run/`、`logs/`、`secrets/`、`tools/` 和 `backups/` 供实例跨版本使用。

先刷新 apt 索引并安装实际存在的包名。Ubuntu 24.04 优先使用 t64 的 GTK 和 ALSA 库，Debian 12 自动选择其传统包名。以下命令逐条成功后再继续，bootstrap 与 build 以运行账户执行。

```bash
sudo apt-get update
bootstrap_packages=$(scripts/bootstrap-box.sh --system-packages)
mapfile -t bootstrap_package_list <<< "$bootstrap_packages"
sudo apt-get install --no-install-recommends "${bootstrap_package_list[@]}"
scripts/bootstrap-box.sh
source "$FERTILE_ROOT/env.sh"
scripts/check-toolchain.sh
scripts/fertile build
```

首次构建完成后，`readlink -f "$FERTILE_ROOT/current"` 应指向本次 checkout 对应的 `releases/<commit>`。构建时间、缓存与重试步骤见 [构建流程](build.md)。

### 控制端 Python 依赖

巡检和进程控制脚本依赖 PyYAML。加载 `env.sh` 后，以运行账户执行 `scripts/setup-supervisor.sh`，按锁定清单将 PyYAML 和 Supervisor 装入 `$FERTILE_ROOT/tools/supervisor`。巡检和日常控制优先使用 `scripts/healthwatch`、`scripts/fertile`，它们会为 Python 控制端选择该实例的解释器。[控制端依赖锁定清单](supervisor-requirements.txt)

需要用发行版的 `/usr/bin/python3` 直接运行脚本时，由管理员通过 apt 安装 PyYAML 对应的 `python3-yaml`，Debian 12 和 Ubuntu 24.04 的包名相同。直接运行 `supervision.py` 还需要 Supervisor 模块。`pip` 安装只在实例虚拟环境内进行。

## 2. 配置行情与管理令牌

创建 `$FERTILE_ROOT/config/runtime.env`，每行写一个非敏感 `KEY=VALUE`。新实例可直接使用下面的 replay 配置。命令会保护已有文件，之后请用编辑器修改并保留其他设置。

```bash
mkdir -p "$FERTILE_ROOT/config"
( set -C; printf '%s\n' 'FERTILE_PROFILE=stable' 'FERTILE_MARKET_SOURCE=replay' \
    > "$FERTILE_ROOT/config/runtime.env" )
```

replay 使用构建中的样例行情，数据与回测队列位于 `$FERTILE_ROOT/data/replay`。paper 模式则在同一个文件内保留 `FERTILE_PROFILE=stable`，将行情源改为 `FERTILE_MARKET_SOURCE=ib`。IB 数据默认写入 `$FERTILE_ROOT/data`。如需独立挂载，在首次启动前创建目录，并将 `FERTILE_DATA_ROOT` 设为实际的绝对路径。默认 paper Gateway 地址为 `127.0.0.1:4002`，可用 `IB_GATEWAY_ADDRESS` 改为管理员配置的本机 paper 地址。

纸面行情需要已授权的 DU 账户、只读 API 和人工确认 2FA。使用 supervisord 时，可按 [Gateway 运维](../ops/ibgateway.md) 安装本机 Gateway、配置 `$FERTILE_ROOT/secrets/ibkr-paper.env` 并由 fertile 启动 Xvfb 和 Gateway。systemd 五项核心服务可连接由运维单独管理的本机 paper Gateway。Gateway 未就绪时，collector 可能返回就绪状态 503，检查 `market/status` 确认原因。

只读看板无需 `WEB_ADMIN_TOKEN`。启用提交回测等写操作时，以运行账户在 `$FERTILE_ROOT/secrets/web.env` 写入唯一的 `WEB_ADMIN_TOKEN` 赋值，文件须为该账户所有的 0600 普通文件。下面的命令首次生成随机令牌，保留已有文件。授权客户端用 `Authorization: Bearer <token>` 请求头提交写操作，令牌由运维通过受控渠道发放。轮换后重启 web。

```bash
install -d -m 0700 "$FERTILE_ROOT/secrets"
umask 077
python3 - <<'PY'
import os
from pathlib import Path
import secrets

path = Path(os.environ["FERTILE_ROOT"]) / "secrets/web.env"
with path.open("x", encoding="utf-8") as stream:
    stream.write(f"WEB_ADMIN_TOKEN={secrets.token_hex(32)}\n")
PY
```

浏览器管理会话和集成凭据按需配置。可选变量、HTTPS origin、权限和默认行为见 [web 管理会话与集成凭据](../docs/configuration.md#web-管理会话与集成凭据)。升级已有数据库时按 [v1 阶段 0 重新部署](../ops/ruler-runbook.md#v1-阶段-0-重新部署) 执行迁移和复验。

## 3. 启动服务

同一实例由一种守护方式控制。[进程清单](services.yaml) 定义两类宿主的启动依赖、就绪、持久路径和限额。安装和启动操作由目标主机的部署账户执行，开发演练在隔离目录或容器中进行。

### systemd

在 systemd 管理的主机上，以运行账户准备控制端的 Python 依赖。若同一实例已有 supervisord 服务，先以运行账户执行 `scripts/fertile down`。管理员审阅安装脚本后，从可信检出安装并启用 `fertile.target`，填写实际的非 root 账户、组和根目录。显式启动 target 后，下次开机由 systemd 拉起 PostgreSQL、finance API、collector、回测 worker 和 web。各 unit 使用 `Restart=always` 恢复自行退出的服务。

```bash
scripts/setup-supervisor.sh  # 准备控制端依赖
sudo scripts/fertile install-systemd --root "$FERTILE_ROOT" --user "$USER" --group "$(id -gn)"
sudo systemctl start fertile.target
scripts/fertile status --json
scripts/fertile logs web
```

由该安装器接管当前根目录时，以运行账户执行 `status`，通过 systemctl 获取进程状态并请求清单中的 live 和 ready 端点。`logs <service>` 读取 journal，`restart <service>` 交由 systemctl 执行。读取 journal 和重启服务需要相应的主机权限。若需提权，管理员可以直接执行 `sudo journalctl -u fertile-web.service -n 100` 或 `sudo systemctl restart fertile-web.service`。

运行参数在启动时从 `config/runtime.env` 读取，修改全栈参数后运行 `sudo systemctl restart fertile.target` 并重新验证。unit 指向安装时选定的基准发布。拥有目标 unit 启停权限的运行账户可按应用就绪与兼容证明逐个切换服务。维护窗口内整体换版时，先用 `sudo systemctl stop fertile.target` 停止整栈，以运行账户构建下一版本，再用同一参数重跑 `install-systemd`，启动 target 并复验。注销这个实例的 unit、停用自启并停止服务时，由管理员执行 `sudo scripts/fertile uninstall-systemd --root "$FERTILE_ROOT"`。[热升级与本机接口](hot-upgrade.md)

私人 Drive 异地备份默认关闭。启用时按手册准备本机加密配置，并为日全量和小时增量安排独立的 timer。 [异地备份调度](../ops/backup.md#调度状态与轮转)

### supervisord

没有 systemd 的环境和现有本地压测盒子可由运行账户加载 `env.sh` 后使用以下命令。首次启动会准备 supervisor 控制端和本机数据库。

```bash
scripts/fertile ensure-up
scripts/fertile status --json
scripts/fertile logs web
scripts/fertile restart web
```

`status` 读取本实例私有 socket 并检查健康端点，`restart` 重启同一个 supervisor 管理的服务，`logs` 从 `$FERTILE_ROOT/logs/<service>/stdout.log` 读取。停止用 `scripts/fertile down`。无开机钩子的环境由运维按 [bootstrap](bootstrap.md) 在重启后恢复。

### tini 容器

镜像提供 `tini`、锁定的 supervisord 控制端和所需运行工具。用 `scripts/fertile container` 作为执行式容器入口，由 tini 接收停止信号并回收子进程，supervisord 在前台管理同一份清单。以非 root 的运行账户挂载持久根，挂载路径在重启前后保持一致，由守护器使用其中的配置、数据、运行文件和日志。容器的终止宽限应覆盖清单中依序停机所需的时间。容器端口映射由运维明确选择，内置进程继续使用环回或 Unix socket。[持久目录与进程边界](hot-upgrade.md#宿主与持久目录)

## 4. 在本机打开看板

web、finance API 和 collector 默认分别监听 `127.0.0.1:8090`、`127.0.0.1:8081`、`127.0.0.1:8082`。受管 PostgreSQL 使用 `$FERTILE_ROOT/run/postgres` 下的 Unix socket，默认端口编号为 5432。端口可在 `runtime.env` 设置 `WEB_PORT`、`FINANCE_API_PORT`、`FINANCE_COLLECTOR_PORT` 和 `FERTILE_POSTGRES_PORT`。以下示例使用默认端口。

```bash
curl -fsS http://127.0.0.1:8090/healthz/live
curl -fsS http://127.0.0.1:8090/healthz/ready
curl -fsS http://127.0.0.1:8090/api/v1/system/status
curl -fsS -o /dev/null http://127.0.0.1:8090/dashboard
```

在部署机的浏览器打开 `http://127.0.0.1:8090/dashboard`。若部署机只有 SSH，可在自己电脑上执行 `ssh -N -L 8090:127.0.0.1:8090 <登录用户>@<服务器>`，再在自己电脑的浏览器打开同一个地址。用 `scripts/fertile status --json` 核对进程和 HTTP 状态。collector 在 paper Gateway 未登录或行情陈旧时可能未就绪，应连同 `system/status` 一起判断。[健康检查](../ops/health-checks.md)

在部署机核对根路径 `/NOTICE` 和第三方许可证路径的状态码及制品正文。[第三方许可证复验](../ops/ruler-runbook.md#第三方许可证复验)

上述步骤都是只读检查。需要验证回测写入时，先配置管理令牌，再以运行账户生成回测样例并运行冒烟。冒烟会提交一条作业。以下用 `--paper-only` 复验核心五项，paper Gateway 恢复中的行情可暂时未就绪。

```bash
(
  FERTILE_DATA_ROOT=$(scripts/fertile data-root) || exit
  export FERTILE_DATA_ROOT FINANCE_BUILD_ROOT="$FERTILE_ROOT/current/runtime"
  cd "$FERTILE_ROOT/current/finance" && PYTHONDONTWRITEBYTECODE=1 bash scripts/backtest-sample.sh
)
tests/e2e-smoke.sh local --paper-only
```

## 5. 可选 HTTPS 反代与公网验证

需要外部访问时，选择 nginx 或 Caddy 在同一台服务器终止 HTTPS。准备目标域名、证书及入口防火墙规则，只将反向代理的 HTTP/HTTPS 端口对外开放。保持 8081、8082、5432 和 web 源站 8090 在本机。把下列 `dashboard.example.com` 替换为站点域名。

nginx 示例使用已签发的证书和私钥。安装 nginx 后，将下段保存到 `/etc/nginx/conf.d/fertile.conf` 并修改证书路径。在 systemd 主机上执行 `sudo nginx -t` 和 `sudo systemctl reload nginx`。

```nginx
server {
    listen 443 ssl;
    server_name dashboard.example.com;
    ssl_certificate /etc/ssl/dashboard/fullchain.pem;
    ssl_certificate_key /etc/ssl/dashboard/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Caddy 可自动获取和续期证书。域名须指向本机，并满足所用 ACME 验证方式的入口要求。安装 Caddy 后，将下段加到 `/etc/caddy/Caddyfile`，用 `sudo caddy validate --config /etc/caddy/Caddyfile` 检查，再由 systemd 重载 `sudo systemctl reload caddy`。

```caddyfile
dashboard.example.com {
    reverse_proxy 127.0.0.1:8090
}
```

未显式配置公网 origin 时仅做本地复验。对已确认代理至本实例的入口，运维可显式检查 `https://dashboard.example.com/dashboard` 和 `/healthz/ready`。完整冒烟还会写入一条回测作业。按上一节准备管理令牌和回测样例后，在维护窗口显式运行 `tests/e2e-smoke.sh public https://dashboard.example.com`。paper 行情处于恢复中时可加 `--paper-only`。部署了 healthwatch 的实例可通过 `FERTILE_PUBLIC_ORIGIN` 或巡检覆盖文件独立启用公网探针。[巡检配置](../ops/monitoring.md#运行与配置)
