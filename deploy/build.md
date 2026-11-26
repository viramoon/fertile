# 构建与本机版本切换

从总仓库执行 `scripts/fertile build --ref <fertile-commit>`。省略 --ref 时选取当前提交。运行账号需能读取指定提交及其三个子模块。已初始化的本地仓库可提供 Git 对象；缺失时按所选提交的 .gitmodules 地址取回。工具链由 [bootstrap](bootstrap.md) 安装。首次源码构建可能耗时较长，工具与下载缓存位于持久目录。

构建脚本在独立目录检出 finance、web、frontend 的 gitlink 和 finance 声明的 Nautilus 提交。finance 按其构建脚本检查工具版本、源码 wheel、SHA256 和本地原生扩展。Rust 二进制以 Cargo.release 和锁文件构建，前端使用冻结的 pnpm 锁文件。每个版本有独立的可移动 Python 运行环境；同一文件系统上的 uv 缓存通过硬链接复用包文件。运行中的版本不会引用下一次构建使用的可写 venv。

finance 的源码 wheel 缓存键包含 `uv.lock` 与构建脚本。缓存键变化且无可用缓存时，为源码构建预留时间。fertile 使用盒子 bootstrap 提供的工具链，发布 venv 按冻结锁文件安装服务所需依赖。

独立运行因子分析时，可使用 finance 的 `scripts/bootstrap-research.sh` 准备工具链。它生成的 `FINANCE_TOOLCHAIN_ENV` 用于选择 finance 构建工具链。源码构建完成后，按上游步骤在独立构建 venv 中同步 `research` 组的 SciPy 和 statsmodels，保留已安装的 Nautilus 源码 wheel。[finance 研究环境](https://github.com/viramoon/finance/blob/327bae28fd8c1f56c44bdcfb086589eaea9583b2/python/README.md#研究环境)

`$FERTILE_ROOT/releases/<fertile-commit>/` 下包括 bin、finance（配置、队列 SQL、Python、真实上游 CSV 示例）、web/config、static、runtime/venv 和 fertile/scripts、fertile/deploy、fertile/VERSION。healthwatch 从发布目录的 VERSION 读取 HTTP User-Agent 版本。manifest.json 记录三个组件的 SHA、Nautilus 源码与 wheel 摘要，以及各目录的 SHA256。相同提交再次 build 时验证安装目录和 Python 原生包，确认通过后复用。日常数据、日志和密钥保存在版本目录之外。

build 与 up、down、rollback 共用 `$FERTILE_ROOT/run/control.lock`。只有全部构建及核验通过后，才移动完整目录并以单次 rename 切换 current。失败保留原 current 和已经部署的版本。修复错误后用同一命令重试。若已有完整目录的摘要不同，build 停止；先保留证据，再用 `scripts/fertile rollback <已验证提交>` 恢复服务。缺包的离线盒子需先恢复 Git、Cargo、uv 和 pnpm 缓存；遇到缓存不匹配时按 finance 源码构建流程补齐。构建不修改 VERSION、tag 或 RELEASES.md。服务如何启停见 [Ruler 手册](../ops/ruler-runbook.md)。

可在无网络的临时目录运行 `tests/build-atomicity.sh`。该测试制造两个并发的失败构建，确认原 current、已有版本和三个脏工作树均被保留。真实成功路径通过两次 build 验证，并比对 manifest.json 和 current 的目标。
