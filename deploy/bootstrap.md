# 云盒子重启后的重建

初始化实现入口为 [scripts/bootstrap-box.sh](../scripts/bootstrap-box.sh)，版本源为 [toolchain.env](toolchain.env)。支持 Ubuntu 24.04 x86_64；不依赖 systemd。所有工具二进制、源码下载、构建缓存和 Postgres 数据在 /home/box/fertile 下，系统包每次检查并补装。

```sh
cd /home/box/fertile-source
bash scripts/bootstrap-box.sh
source /home/box/fertile/env.sh
bash scripts/check-toolchain.sh
bash scripts/postgres.sh init
bash scripts/postgres.sh start
bash scripts/postgres.sh status
```

第二次安装全部版本正确时跳过，不重新下载。失败保留缓存，可重跑；已有数据库绝不删除，主版本不兼容时停止并要求迁移。Postgres 的日志在 logs/postgres.log，socket 在 run/postgres，数据在 data/postgres；停止用 scripts/postgres.sh stop。FERTILE_POSTGRES_PORT 可覆盖默认 5432，CI 用 55432。

只有系统依赖可进入系统目录：编译器基础库、make / pkg-config、SSL / zlib、终端 / XML / Z3 运行库、bison / flex、curl / git / xz / bzip2 / CA。Rust / Python / uv / Node / pnpm / LLVM / Postgres 的安装不依赖系统目录保存，env.sh 每次可重建，source 不修改用户 HOME。

Ruler 后续仍须：按选定 fertile tag 恢复组件与匹配静态资源 → 读取受控密钥与分层配置 → 恢复 Gateway 与采集 → 完成交易状态对账 → 满足 ready 后开放 API / web / Tunnel。bootstrap 只准备工具与数据库，不启动业务服务、不执行应用迁移、不登录 Gateway，也不实现常驻守护。部署与回滚始终以 fertile tag 为单位。

详细上手、版本同步与 CI 见 [开发环境](../docs/dev-environment.md)，密钥见 [配置规范](../docs/configuration.md)，Gateway 见 [部署方案](../docs/ibkr-gateway.md)，网络与迁移原则见 [ADR-001](../docs/adr/ADR-001-cloud-box-network-constraints.md)。
