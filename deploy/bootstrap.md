# 云盒子初始化与重启恢复

支持 Debian 和 Ubuntu 24.04 的 x86_64 盒子，以 box 用户操作。首次部署从有权读取三个私有子模块的 checkout 开始。持久根目录默认为 /home/box/fertile，源码目录与运行目录分开。

```sh
cd /home/box/fertile-source
git submodule update --init --recursive
bash scripts/bootstrap-box.sh
source /home/box/fertile/env.sh
bash scripts/check-toolchain.sh
scripts/fertile build
scripts/fertile up
scripts/fertile status --json
```

bootstrap 可重入，系统包按需重装，锁定工具与 PostgreSQL 安装在持久目录。已有 PostgreSQL 数据目录须与所用主版本一致。业务启动由 supervisord 管理，手工操作 scripts/postgres.sh 之前先检查进程所有者，避免两个启动入口争用同一数据目录。

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
