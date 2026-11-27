# 单进程热升级与本机接口

从已验证的完整 fertile 制品中选择一个服务换版。其余进程沿用自己的版本，采集器在 finance API、web 或 projector 换版期间继续运行。读者先检查候选的就绪和游标，再切换。finance API 和 web 使用临时环回端口启动候选，最终在原服务入口更换进程，入口可能短暂不可用。

## 宿主与持久目录

[进程清单](services.yaml) 提供命令、启用条件、启动依赖、健康检查、卷用途和限额。普通服务器通过 systemd 安装。容器使用 `scripts/fertile container` 以 tini 启动前台 supervisord，云盒子也可直接使用 supervisord。两种守护器使用同一个实例根，部署账户应能操作所选进程。systemd 热升级还需对目标 `fertile-*.service` 的启停权限。核心端口、容器入口和安装步骤见[普通服务器部署指南](linux-server.md)。

| 路径 | 用途 |
| --- | --- |
| `releases/{sha}` | 只读发布制品，每批包含锁定的三个组件及对应的前端产物。`current` 或已安装的 systemd 记录提供整栈基准。 |
| `config/service-releases.json` | 控制器写入单进程发布指针，作用域为记录的基准提交。重启服务时读取与该基准匹配的指针。保留此文件时，再次选择相同基准提交会重新应用相应指针。 |
| 实例数据根 | collector 写获准持久化的源记录，projector 消费已提交记录并更新投影及游标，finance API 读取业务数据与投影，回测 worker 和 API 共用持久队列。 |
| `data/postgres` 与 `run/postgres` | 本机 PostgreSQL 的数据与 Unix socket。web 及 notifier 使用同一个 web 库。 |
| `logs/` 与 `run/` | supervisord 日志和 socket、守护状态及互斥锁。`run/handoff-start/` 中的一次性许可由当前控制进程发放，仅供对应服务和发布启动一次。systemd 日志由 journal 保管。 |
| `state/hot-upgrade/` | 热升级事务，记录步骤、已验证证据和 collector 交接缺口。保留在实例持久盘。 |

持久目录应在源码树外。清单中的卷用途供部署挂载和权限核对。默认优雅退出宽限为 30 秒，回测和 projector 为 60 秒，notifier 为 75 秒。核心 HTTP 进程的文件描述符限额为 4096，回测 worker 为 8192。容器终止宽限应容纳进程顺序停止所需的时间。systemd 与 supervisord 的就绪和限额以清单及各自渲染结果为准。[数据发布政策](../docs/data-policy.md)

| 进程 | 启用与交接 |
| --- | --- |
| `finance-api`、`web` | 环回 HTTP 读者候选检查 live、ready 和运行时兼容性。web 候选使用本次发布的静态制品。 |
| `finance-collector` | 从实例配置选择 `ib` 或 `replay`。IBKR 只允许 paper 只读，切换中维持单一订阅拥有者。 |
| `finance-collector-binance`、`finance-collector-hyperliquid` | 独立行情源的部署模板，默认关闭。Binance 的 finance 制品为 `bin/finance-binance`，`cloudbox` 和 `stable` 配置均为 `enabled: false`。首次启用前确认产品用途许可与来源交接条件。 |
| `finance-projector` | F3 制品带 `fertile_finance.market_projector` 和 `bin/finance-market-wal` 时，可用 `FERTILE_ENABLE_FINANCE_PROJECTOR=1` 显式启用。使用 WAL 水位与投影游标判断就绪，活动投影的发布权只交给一个进程。 |
| `finance-backtest-worker` | 现有 launcher 接入所选发布的 F13 worker 及同一发布的 `finance-strategy`。worker 启动时创建 SQLite `job_leases`，租约默认 15 秒。升级先排空并停止旧 worker，再启动新版。 |
| `finance-backtest-worker-f13`、`finance-live-decision` | 独立模板均默认关闭。启动 `finance/scripts/finance-decision` 需要已获准计算和留存的 `--plan`、`--replay` 及绝对数据根。决策进程无端口，输出到本地 `finance-state/decisions/decisions.sqlite3`。 |
| `web-notifier` | W3 进程，以 `FERTILE_ENABLE_WEB_NOTIFIER=1` 显式开启。制品需带 `bin/web-notifier`。依赖同一目标库完成 `web migrate`、系统 CA、Linux `/proc` 与支持 SMTP/SMTPS 的 `/usr/bin/curl`。无监听端口，投递状态留在 outbox。 |

W3 配置由部署账户将 SMTP 中继、固定发件人、自己的邮箱及可选的凭据文件引用写入属主可读写的 0600 `secrets/web-notifier.env`。启动时同时加载 web 的数据库设置。受管 PostgreSQL 的 W3 迁移状态在启动前做只读检查，外部 PostgreSQL 由数据库管理员按 web 的迁移流程准备。等待当前邮件尝试结算的时限见清单。先在隔离实例复验 outbox 和停止行为，再决定何时启用。[W3 邮件进程](https://github.com/viramoon/web/blob/4c966be/RUNBOOK.md#告警与邮件进程)

F3 投影候选使用隔离的投影状态和 catalog。用 `finance-market-wal watermarks` 核对每个产品的 `local_durable` 与 `projected`，并验证新版候选能达到原游标。完成检查后，先确认旧投影释放产品发布锁，再让新进程接管共享状态。生产者接入 F3 时由原进程使用 `MarketIngress::seal()` 提交在线封段并交接，质量恢复结论来自源适配器。旧 bar collector 在接好这些步骤前，沿用各自的采集与就绪接口。[F3 WAL 和投影格式](https://github.com/viramoon/finance/blob/d295d6c/docs/market-wal.md)

## 升级与兼容回退

首次采用本控制器时，先在维护窗口安装包含启动许可检查的整栈基准。热切换沿用基准的进程拓扑、运行卷、限额、持久根和端口。变更这些条件时安排整栈维护。直接调用 systemctl 启停及 systemd 安装和卸载均安排在热升级事务之外。要用同一发布统一启动全部服务，在停栈的维护窗口先将 `config/service-releases.json` 移入私有归档，再启动所选基准。

1. 保留两版制品和可恢复的升级前备份。用已锁定的子模块构建完整候选并保留原基准。候选必须使用完整 fertile 提交号。

   ```sh
   scripts/fertile build --stage-only --ref "$candidate"
   ```

2. 在独立数据根和独立数据库演练旧版读原数据、新版读原数据及旧版读扩展后的数据。核对 API 与消费者、旧回测摘要、队列与租约、已验证 WAL 及投影水位。验证环境只接合成或回放 fixture，保留版本、计数和通过状态。出现破坏性变更时安排独立迁移窗口和隔离恢复目标。应用 schema 先扩展、兼容读取，回填和切读后另期清理旧字段。
3. 在目标实例记录服务 PID、路由、当前版本、数据根和采集水位。准备应用特定的可执行 hook，由其读取本机已授权的状态并实现下节接口。需要扩展迁移时加 `--migrate`，hook 应按目标数据库的权限流程应用迁移。web 热切换时关闭自动迁移，通过已验证的扩展步骤管理版本。逐个服务执行，先准备并验证其依赖。

   ```sh
   scripts/fertile hot-upgrade finance-api "$candidate" --hook "$approved_hook"
   scripts/fertile hot-upgrade-status --json
   ```

4. 对读者核对原入口恢复就绪、选中的制品及游标，再检查 collector 进程仍在且水位持续推进。控制器先确认候选进程拥有临时监听端口并通过 live、ready，然后仅替换目标进程，再核对原入口的进程归属与就绪。对 collector 按源和产品记录封段截点、租约释放、重新订阅和缺口结论。切换 collector 期间存在采集窗口，系统记录恢复程度。
5. 对已证明可兼容同一数据根的旧制品，使用同样的角色就绪或交接流程回退。回退保留扩展后的数据与迁移账本。回读失败时保留两套数据，沿用隔离恢复流程。[从升级前快照回滚](../ops/ruler-runbook.md#从升级前快照回滚并切回新版)

   ```sh
   scripts/fertile hot-upgrade finance-api "$previous" --rollback --hook "$approved_hook"
   ```

发生中断时先查看 `hot-upgrade-status`，依据实际管理器、服务发布、候选进程、队列及单写者归属核对状态，再执行 `scripts/fertile hot-upgrade-reconcile "$transaction_id" --hook "$approved_hook"`。操作账户负责清理外部启动的 projector 候选。只接受与前后发布之一相符的恢复证据。若目标服务已停止，hook 必须检查租约与未完成写入，并以 `safe_to_start:true` 允许控制器启动所选版本。supervisord 已退出时，恢复命令会重新拉起守护器，并按需启动该服务的依赖。其它进程待事务完成后走常规启动流程。

若实例的 `ib` collector 依赖同一 supervisord 管理的 paper Gateway，恢复依次启动缺失的 Xvfb 和 Gateway，再启动 collector。collector 已运行时重试也会补齐停止的依赖。Gateway 登录或 2FA 尚未完成时，collector 保持未就绪，事务可在确认登录后重试。Replay 与由运维单独管理 Gateway 的实例沿用各自的启动流程。

未完成的事务阻止两类守护器自行启动受影响服务。控制器仅在对应交接步骤或确认安全恢复后发放一次性启动许可。每次 reconcile 都要通过应用的 `reconcile-ready`，包括上次启动后就绪失败、重试时 PID 仍存在的情况。中断且未证实恢复的采集区间仍标记连续性未知。常规整栈切换与启动问题见[Ruler 手册](../ops/ruler-runbook.md#整栈维护)。

若整个容器已退出，可在使用同一持久根和服务账户的维护进程中执行恢复。先确认原容器和守护器已经停止。恢复完成后退出维护环境，确认其守护器停止，再重启正常容器。

旧基准的启动器缺少启动许可检查。若已留下未完成的旧事务，先停旧整栈并停用其自启，保留原事务、发布指针和前后两版制品。维护窗口内暂存包含保护的基准，在停栈状态和实例控制锁下切换 `current`，把核实过的逐服务发布选择迁到新基准。systemd 重新安装 unit 后保持停栈，使用新控制器恢复原事务。完成后恢复常规启停。旧事务继续使用其原 ID 和已有证据。

## 应用 hook 协议 v1

hook 是部署账户认可的绝对路径可执行文件，使用常规文件，组和其他用户不可写。每次调用传入步骤名作为第一个参数，通过 stdin 接收一个 JSON 对象，通过 stdout 返回一个 UTF-8 JSON 对象。stdout 总计最多 65,536 字节，包含换行，时限为 120 秒。错误用非零退出码返回。请求包含 `version`、`id`、`service`、`role`、`source`、`previous`、`target`、`direction`、`migrate`、`step`、`evidence`、`gap` 和 `status`。`previous` 和 `target` 是完整 fertile SHA，`source` 用于限定被升级的采集源。`evidence` 只包含同事务已经验证的步骤结果。输出仅包含应用健康、兼容性及水位元数据，日志和证据不包含凭据、原始行情或邮件内容。

控制器启动进程后的就绪步骤收到临时的 `runtime` 对象，含已核验的 `pid`、`live_url` 和 `ready_url`。无 HTTP 入口的进程对应 URL 为空。应用可据此核对候选或正式进程的水位和队列状态，控制器仅持久化通过验证的应答字段。

| 步骤 | 输入约束与成功字段 |
| --- | --- |
| `compatibility` | 检查两版对共享数据、迁移状态与依赖的兼容，并用真实旧版读取路径验证回退条件后返回 `{"compatible":true}`。 |
| `migrate` | 显式执行扩展迁移，保留旧版读取与写入所需的形态，确认后返回 `{"expanded":true}`。 |
| `reader-checkpoint` | 返回 `watermark` 字典，键为 `source/product`，值为本地 WAL 水位的十进制字符串。projector 使用非空水位。 |
| `candidate-start`、`candidate-ready` | 控制器启动 HTTP 读者候选。projector hook 以隔离状态启动候选并返回 `{}`。候选就绪后 hook 返回 `{"ready":true,"watermark":{...}}`，各产品水位至少达到原检查点。 |
| `reader-cutover`、`reader-ready`、`candidate-abort` | 控制器回收 HTTP 候选，projector hook 负责回收隔离候选。projector 切换时，控制器收到 `{}` 确认后才停止旧 projector 并启动新进程。应用在原入口重新就绪后返回 `ready:true` 与 `watermark`，水位继续满足检查点。切换前失败时，按同样分工终止候选并清理隔离状态。 |
| `collector-seal`、`collector-release` | hook 让旧写者停止摄取并封段，返回 `watermark`。控制器停旧进程后，hook 验证写者锁及来源订阅已释放，返回 `released:true` 与最终 `watermark`。仅展示源可在封段时返回 `display_only:true` 与空 `watermark`，此后该事务沿用空水位。 |
| `collector-start`、`collector-ready`、`collector-gap` | 控制器在确认交接证据后启动新写者。就绪后 hook 返回 `ready:true` 与 `watermark`。按产品核对缺口，返回 `products`，以 `source/product` 为键，每项含 `continuity` 和可选的 `missing_count` 十进制字符串。 |
| `worker-drain`、`worker-stop`、`worker-start`、`worker-ready` | 排空后 hook 返回 `drained:true`，控制器停启目标 worker。应用确认可继续消费同一队列后返回 `ready:true`。启用的 notifier 使用相同的交接流程，检查 outbox 和当前投递尝试。 |
| `reconcile` | 只读核实实际进程和持有者，返回 `reconciled:true` 与 `active_release`。若所选服务已停且确认可安全启动，另返回 `safe_to_start:true`。 |
| `reconcile-ready` | 每次恢复确认均调用，包含现存进程。检查应用就绪后返回 `{"ready":true}`。 |

采集缺口的 `continuity` 取值为 `unknown`、`ticks-restored`、`bars-restored`、`snapshot-only` 或 `unrecoverable`。封段、释放或新进程就绪水位里出现的每个产品，均须在 `products` 中有对应结果。`unknown` 的计数必须为空。仅展示的同源产品也可提交产品级结果。既有来源级应答仍适用于零个或一个已知产品。多产品事务以 `gap.products` 为准，来源级摘要保持未知和空计数。

本地单调水位可证明写入进度。源连续性需要源级序号或补数证据，例如 Binance 重建快照后的更新 ID、Hyperliquid 快照与成交覆盖、IBKR 市场深度 reset 与可取得的历史窗口。事务的 `gap.opened_at` 和 `gap.ready_at` 是控制器观察到的封段前和新进程就绪后时间。

T3a 的五个核心服务入口继续沿用，其中回测 launcher 加载 F13 worker。带 F3、W3 制品的发布支持各自的显式启用开关。独立行情、额外 worker 和实时决策模板维持默认关闭。交付验证使用隔离进程和生成模板，真实 paper 的连续性由获授权人员另在私有实例核实。[T3a 运行时核对](../ops/ruler-runbook.md#v1-t3a-首批运行时核对)

## 隔离验证

演练只使用临时根、合成输入和本机环回。守护 CI 及集成 CI 要求实际入口演练通过。依赖准备与演练分开执行。[守护 CI](../.github/workflows/supervisor.yml) · [集成 CI](../.github/workflows/integration.yml)

- `python -B tests/container_hot_upgrade_test.py --require-native` 以锁定的控制端 Python 运行真实 `scripts/fertile container` 入口。用进程树核对 tini、控制器和 supervisord，再用真实 `hot-upgrade` 切换及回退 API。核对候选读到的水位、原端口恢复以及同一 collector 的追加序列贯穿切换。集成 CI 用 `--finance-api` 和 `--finance-config` 传入构建产物及其配置，采集端仍使用合成生成器。[容器与热切换演练](../tests/container_hot_upgrade_test.py)
- `python3 -B tests/systemd_process_test.py --image fertile-systemd-drill` 使用预先准备的镜像，在禁网的独立容器内运行 systemd。生产安装入口生成 unit，容器内管理器实际启动、重启和停止合成进程。核对 cgroup、运行账户、限额、collector PID 和 TERM 退出证据。只校验夹具时使用 `--validate-fixture`。[原生 systemd 演练](../tests/systemd_process_test.py)
- `python3 -B tests/published_queue_readback.py --finance-source PATH --candidate BINARY` 从本地 Git 对象离线构建此前发布的 finance `7600be4`，与本次构建的 API 分别运行。集成 CI 在依赖准备阶段用同一脚本的 `--fetch-published-dependencies TARGET` 按宿主平台预取已发布 gitlink 对应的 `Cargo.lock`，并预取当前 gitlink 的依赖。演练构建仍使用 `cargo build --offline --locked`。先由旧版写入作业，扩展独立 SQLite 夹具，再经新版 API 写入另一作业。重启旧制品后，由其 HTTP 读回新版写入的作业并列出两条记录。缺少 socket 的环境可用 `--build-only` 核对两份制品。[已发布制品回读演练](../tests/published_queue_readback.py)
- 快速回归覆盖合成水位、单写者释放、半段恢复、按产品记录缺口和中断重试。paper 冷恢复使用模拟 Gateway 和就绪状态。可用 `scripts/fertile verify-systemd` 检查渲染的 unit。[热升级事务演练](../tests/hot_upgrade_test.py) · [命令与管理器演练](../tests/hot_upgrade_cli_test.py) · [中断恢复](../tests/hot_upgrade_recovery_test.py)
