# 开发与云盒子环境

版本的唯一维护源为 [deploy/toolchain.env](../deploy/toolchain.env)。本批选择 NautilusTrader v1.231.0（27a8e54e7ac3c57d6cbf8891f0283dfbaee97317），这是 GitHub Latest 且 prerelease=false 的 release；项目自身仍标 Beta。其 rust-toolchain / MSRV 为 1.97.1，Python 范围 >=3.12,<3.15，uv 必须 0.11.33。上游要求 clang 与 lld，未指定最低 clang 版本；本系统锁定 LLVM 22.1.8。其他精确版本见该文件，不在本文维护第二张版本表。

## 新盒子与重启后

当前支持 Ubuntu 24.04、Linux x86_64，Bash 与 sudo 可用。只有 /home/box 按持久处理；PID 1 是 tini，不依赖 systemd。

```sh
git clone --recurse-submodules https://github.com/viramoon/fertile.git /home/box/fertile-source
cd /home/box/fertile-source
bash scripts/bootstrap-box.sh
source /home/box/fertile/env.sh
bash scripts/check-toolchain.sh
bash scripts/postgres.sh init
bash scripts/postgres.sh start
bash scripts/postgres.sh status
```

停止数据库用 `bash scripts/postgres.sh stop`。初始化、启动和停止允许重复执行；已有数据主版本不匹配时拒绝自动迁移。默认仅监听 127.0.0.1:5432；本地 Unix socket 在持久根的 run/postgres，CI 使用 55432。

安装器以 FERTILE_BOX_HOME 覆盖 /home/box，例如 `FERTILE_BOX_HOME=/tmp/ci-box bash scripts/bootstrap-box.sh`；后续命令也使用相同变量。它必须是无空格的绝对路径。不修改 HOME / 用户 shell 配置；env.sh 不含密钥。Rustup / Cargo、uv / Python、Node、pnpm、LLVM、PostgreSQL 二进制、缓存和数据都在此根下，系统目录没有这些工具的唯一副本。

系统包是可重建的编译与运行依赖：build-essential、pkg-config、libssl-dev、zlib1g-dev、libtinfo6、libxml2、libz3-4、bison、flex，以及 curl / git / xz / bzip2 / CA。每次检查 dpkg；缺失才通过 apt 补装，重启后丢失也可重建。sudo 需允许保留代理变量，安装器不打印代理值或凭据。网络下载带重试、超时和原子落盘；失败保留缓存并明确提示重跑。LLVM 归档只展开 clang / lld、共享库、libclang 和资源文件，减少空间。pnpm 12 直接安装官方 Linux 原生包，避免 npm 引导入口在首次执行时额外联网下载。

## 跨仓库同步

finance 的 rust-toolchain.toml、.python-version、.uv-version、toolchain.env 是派生文件，由 `python scripts/sync-toolchain.py /path/to/fertile/deploy/toolchain.env` 生成。修改版本必须改 fertile 维护源并同批更新子仓库；检查模式加 --check。后续 web / frontend 同理，.nvmrc、packageManager 与维护源一致。源文件、锁文件与生成步骤同 PR 更新，不手改生成文件。finance 当前按源码 submodule 构建，见其 ADR-0003。

## CI 策略

所有仓库只使用 GitHub 托管 runner，不在云盒子自建 runner。bootstrap CI 在干净 ubuntu:24.04 容器内运行：首次真实安装、二次全 SKIP、source 后逐工具核对精确版本与前缀、Postgres 建表写入并跨重启读取。日志与冷 / 热耗时作为 artifact 保存，不能用 mock 代替安装验收。

finance CI 从上游源码构建 wheel，缓存以 source commit + 工具链 / lock / 构建脚本为依据，并运行上游离线最小示例。缓存只能加速，不能跳过来源校验与回测。部署与 CI 相互独立：CI 不访问 IBKR 账户、不登录 Gateway、不执行生产迁移。密钥与配置见 [configuration.md](configuration.md)，Gateway 方案见 [ibkr-gateway.md](ibkr-gateway.md)。
