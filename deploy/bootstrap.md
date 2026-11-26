# 工具链引导与云盒子恢复

支持 Debian 12 和 Ubuntu 24.04（x86_64），从公开的 fertile 仓库检出源码，以实际运行账户操作。源码目录与运行根分开。bootstrap 按 `FERTILE_ROOT`、`$FERTILE_BOX_HOME/fertile`、profile 默认值的顺序选根。普通 Linux 服务器使用 `stable`，默认 `/var/lib/fertile`，安装与启动见 [普通服务器部署指南](linux-server.md)。云盒子使用 `cloudbox`，默认 `/home/box/fertile`。

以下命令从云盒子上的源码目录执行，已有自定义路径时继续使用该实例的设置。未设置 `FERTILE_ROOT` 时，bootstrap 会在最后一行报告 `env.sh` 的实际路径。

```sh
export FERTILE_PROFILE=cloudbox
export FERTILE_ROOT="${FERTILE_ROOT:-${FERTILE_BOX_HOME:-/home/box}/fertile}"
git submodule update --init --recursive
bash scripts/bootstrap-box.sh
source "$FERTILE_ROOT/env.sh"
bash scripts/check-toolchain.sh
scripts/fertile build
scripts/fertile up
scripts/fertile status --json
```

bootstrap 可重入，系统包按需重装，锁定工具与 PostgreSQL 安装在持久目录。GTK 和 ALSA 在 Ubuntu 24.04 优先选择可用的 t64 包，在 Debian 12 选择传统包名。运维可以用 `scripts/bootstrap-box.sh --system-packages` 解析包名、提前安装系统依赖。已有 PostgreSQL 数据目录须与所用主版本一致。云盒子启动由 supervisord 管理。手工操作 `scripts/postgres.sh` 之前先检查进程所有者，避免两个启动入口争用同一数据目录。

运维脚本需要 PyYAML。加载 `env.sh` 后可运行 `scripts/setup-supervisor.sh`，将锁定的 PyYAML 和 Supervisor 安装到 `$FERTILE_ROOT/tools/supervisor`。日常使用 `scripts/healthwatch`、`scripts/fertile`。如需直接调用系统 Python，由管理员通过 apt 安装 `python3-yaml`。[控制端 Python 依赖](linux-server.md#控制端-python-依赖)

重启后若系统依赖丢失，重新执行 bootstrap 并加载 env.sh，然后运行 scripts/fertile ensure。盒子没有已确认的持久启动钩子；Maas · Ruler 在重启后和定期巡检中执行 ensure 并检查各服务 ready。应急、回滚和外部访问验证见 [Ruler 手册](../ops/ruler-runbook.md)。

## PostgreSQL 时区

首次 `initdb` 时，PostgreSQL 根据当时的系统时区设置 `TimeZone` 和 `log_timezone`。已有集群保留初始化或后续显式设置的值。若要指定集群默认时区，在数据库运行时用 PostgreSQL 管理角色连接本机 Unix socket。下例设为 `Asia/Shanghai`，自定义实例须把 `5432` 换成实例端口。

```sh
psql -h "$FERTILE_ROOT/run/postgres" -p 5432 -d postgres -v ON_ERROR_STOP=1 \
  -c "ALTER SYSTEM SET timezone = 'Asia/Shanghai'" \
  -c "ALTER SYSTEM SET log_timezone = 'Asia/Shanghai'" \
  -c 'SELECT pg_reload_conf()'
psql -h "$FERTILE_ROOT/run/postgres" -p 5432 -d postgres -v ON_ERROR_STOP=1 \
  -c 'SHOW TimeZone' -c 'SHOW log_timezone'
```

两项设置持久保存于当前集群。用第二条命令的新连接确认重载已经生效。
