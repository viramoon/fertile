# 开发与云盒子环境

版本的唯一维护源为 [deploy/toolchain.env](../deploy/toolchain.env)。本批选择 NautilusTrader v1.231.0（27a8e54e7ac3c57d6cbf8891f0283dfbaee97317），这是 GitHub Latest 且 prerelease=false 的 release；项目自身仍标 Beta。其 rust-toolchain / MSRV 为 1.97.1，Python 范围 >=3.12,<3.15，uv 必须 0.11.33。上游要求 clang 与 lld，未指定最低 clang 版本；本系统锁定 LLVM 22.1.8。其他精确版本见该文件，不在本文维护第二张版本表。

## 新盒子与重启后

支持 Debian 12 和 Ubuntu 24.04（x86_64），需要 Bash 和 apt 安装权限。下面示例针对独立开发实例。压测盒子的 PID 1 是 tini，只有 `/home/box` 按持久处理，继续使用 supervisord。普通服务器的安装步骤见 [普通服务器部署指南](../deploy/linux-server.md)。

```sh
git clone --recurse-submodules https://github.com/viramoon/fertile.git
cd fertile
export FERTILE_ROOT="$HOME/fertile-dev"
bash scripts/bootstrap-box.sh
source "$FERTILE_ROOT/env.sh"
bash scripts/check-toolchain.sh
bash scripts/postgres.sh init
bash scripts/postgres.sh start
bash scripts/postgres.sh status
```

停止手工启动的数据库用 `bash scripts/postgres.sh stop`。初始化、启动和停止允许重复执行，已有数据主版本不匹配时拒绝自动迁移。手工 `start` 默认仅监听 `127.0.0.1:5432`，supervisord 和 systemd 启动的受管数据库只用持久根 `run/postgres` 下的 Unix socket。CI 使用端口编号 55432。

PostgreSQL 首次初始化时采用系统时区。持久集群的显式设置与验证步骤见 [PostgreSQL 时区](../deploy/bootstrap.md#postgresql-时区)。

已导出的 `FERTILE_ROOT` 优先。用 `FERTILE_BOX_HOME=/tmp/ci-box bash scripts/bootstrap-box.sh` 可以将默认根改为 `/tmp/ci-box/fertile`，后续命令加载该根下的 `env.sh`。bootstrap 接受无空格的绝对路径。`env.sh` 提供工具路径和缓存配置，保留现有 HOME 与用户 shell 配置。Rustup / Cargo、uv / Python、Node、pnpm、LLVM、PostgreSQL 二进制、缓存和数据都在持久根下。

系统包是可重建的编译与运行依赖。用 `scripts/bootstrap-box.sh --system-packages` 查看 apt 选择的包名。Ubuntu 24.04 优先选用 GTK、ALSA 的 t64 包，Debian 12 回退到传统包名。每次用 dpkg 检查安装状态，缺失时通过 apt 补装。若用 sudo 补装，需允许保留代理变量。网络下载带重试、超时和原子落盘，失败保留缓存。LLVM 归档只展开 clang / lld、共享库、libclang 和资源文件，减少空间。pnpm 12 直接安装官方 Linux 原生包。

## 跨仓库同步

finance 的 rust-toolchain.toml、.python-version、.uv-version、toolchain.env 是派生文件，由 `python scripts/sync-toolchain.py /path/to/fertile/deploy/toolchain.env` 生成。修改版本必须改 fertile 维护源并同批更新子仓库；检查模式加 --check。后续 web / frontend 同理，.nvmrc、packageManager 与维护源一致。源文件、锁文件与生成步骤同 PR 更新，不手改生成文件。finance 当前按源码 submodule 构建，见其 ADR-0003。

## CI 策略

所有仓库只使用 GitHub 托管 runner。bootstrap CI 在干净的 `ubuntu:24.04` 容器内执行首次真实安装、二次全 SKIP、source 后逐工具核对精确版本与前缀，以及 PostgreSQL 建表写入并跨重启读取。冷 / 热耗时通过公开报告检查后作为 artifact 保存。Debian 12 检查用该版本的 apt 索引验证包名解析。systemd CI 渲染并静态校验 unit，守护行为由隔离测试验证。Git 与制品检查见 [四仓数据发布政策](data-policy.md)。

finance CI 从上游源码构建 wheel，缓存以 source commit + 工具链 / lock / 构建脚本为依据，并运行上游离线最小示例。缓存只能加速，不能跳过来源校验与回测。部署与 CI 相互独立：CI 不访问 IBKR 账户、不登录 Gateway、不执行生产迁移。密钥与配置见 [configuration.md](configuration.md)，Gateway 方案见 [ibkr-gateway.md](ibkr-gateway.md)。

总仓 Actions 先使用内置令牌检出 fertile。管理员在 fertile 的 Settings → Secrets and variables → Actions 新增仓库级 FERTILE_SUBMODULES_TOKEN，选择能够读取 finance、web、frontend 的细粒度凭据，三个仓库仅授予 Contents: Read 并按所属组织规则审批。重跑集成作业。诊断依次检查 secret 配置、仓库可读性和锁定提交可达性；Nautilus 源码来自公开上游。验证远端祖先、从源构建三组件、重导出两份契约及重跑两个客户端生成器后，在专用运行根启动 PostgreSQL、回放行情、finance 与 web，执行真实队列和页面冒烟。备份恢复、Gateway 离线保护、巡检与 supervisor 各自有独立作业；这些作业只需读取总仓。

集成作业在临时 PostgreSQL 中验证旧连接布局下的空库可由真实的 `web migrate` 初始化。作业另行恢复仅含 W1 前迁移记录的库，验证受管角色应用 `20261126000000`、保留既有数据并支持重复迁移。

### 总仓集成作业的 Nautilus wheel 缓存

[集成工作流](../.github/workflows/integration.yml) 先完成锁定工具链的安装和校验，再按精确 key 恢复 `$FINANCE_BUILD_ROOT/wheels`。该目录含源码构建的 wheel、`SHA256SUMS` 和 `source-commit`。key 将 finance gitlink（含构建脚本、锁文件、Nautilus gitlink）与总仓工具链配置、bootstrap 与发布构建脚本、工作流、runner 镜像版本、系统库版本绑定，并包含构建模式、精度、架构、Rust 和 Clang 版本输出及 Python SOABI。命中后，finance 仍按冻结锁文件同步环境，校验 wheel 的 SHA256 与源码提交，重新安装并导入原生扩展。发布构建也会验证 wheel 与发布目录。

CI 在 bootstrap 后清理安装归档与 PostgreSQL 源码。构建成功后清理 Nautilus 专用的 Cargo 产物和源码工作树，记录缓存目录大小及 `df -h`，随后在契约比对和部署演练前保存本次新生成的 wheel。finance、web 二进制和前端仍按各自锁文件构建，契约重导出会复用同一作业的总仓 Cargo 目录。

验证缓存时，先等首次作业的保存步骤记录成功和归档大小，再对同一个 PR 的同一提交重跑。用 `NATIVE_WHEEL_CACHE_HIT=true` 确认 Actions 恢复，用 `BUILD_CACHE_HIT=true` 确认 finance 复用，并对比 `BUILD_SECONDS`、`BOOTSTRAP_SECONDS` 和 `RELEASE_BUILD_SECONDS`。PR 写入的缓存属于该 PR 的合并引用。PR 也可以恢复默认分支和该 PR 基准分支的匹配条目。runner 镜像或锁定输入变更会生成新的 key。
