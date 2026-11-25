# IBKR paper Gateway

云盒子通过原生 IB Gateway 10.50.1g、IBC 3.24.2 和 Xvfb 提供只读 paper API。supervisor 管理 `xvfb` 与 `ib-gateway`。只有 `ib` 行情来源需要这两个进程。Replay 验收可以独立进行。可执行命令及恢复步骤见 [操作手册](../ops/ibgateway.md)。

## 安装和持久化

版本和两个 SHA256 固定在 [toolchain.env](../deploy/toolchain.env)。[安装器](../scripts/install-ibgateway.sh) 下载指定版本的 gnzsnz/ib-gateway-docker 安装包归档与 IbcAlpha 的 IBC zip，逐个校验后先在同一文件系统解包，再安装到 `tools/ibgateway/<版本>` 及 `tools/ibc/<版本>`。默认根目录为 `/home/box/fertile`。Gateway 使用安装包自带的 JRE。[bootstrap](../deploy/bootstrap.md) 负责重装可丢弃的 Xvfb、X11、字体及 AWT 依赖。

Jts 设置、IB 自身保存的认证状态位于 `data/ibgateway`，限次重试与进程身份位于 `run/ibgateway`，脱敏事件位于 `logs/ibgateway/events.jsonl`。以上状态由运行用户拥有。用于 IBC 的 0600 配置仅在启动与运行期间存在。应排除整个凭据目录、IB 登录状态及运行时配置的备份和上传。

## 访问边界

服务只读取 `/home/box/fertile/secrets/ibkr-paper.env`。IBC 固定 paper、只读、端口 4002、拒绝未经许可的连接；Xvfb 使用本地 Unix socket 和私有 Xauthority cookie。IBC `BindAddress` 控制的是 IBC 自身的命令服务，不能用来声明 API 的监听地址。命令服务已关闭。

安装器使用系统 C 编译器构建 [Linux 监听限制](../deploy/ibgateway/loopback.c)，仅对 IBC/JVM 子进程设置 `LD_PRELOAD`。Gateway 的 IPv4 4002 绑定固定为 127.0.0.1；实盘 4001 和 IPv6 的 4002 绑定会失败。启动器在提交凭据前通过同一动态加载环境实际执行绑定，加载或验证失败即停止。健康检查还会检查内核 IPv4/IPv6 监听表。发现非回环 4002 或实盘 4001 时，Gateway 停止并要求人工处理。规则不会变更盒子的防火墙或其他服务的环境。

`ib-check.sh` 调用同一部署中的 `finance-ib probe`，验证 server time 和全部账户的 DU 前缀。输出只含时间、前缀和数量。API 和 X11 均不配置公网 Tunnel。行情订阅、行情权限、长时间实时连接及订单权限应分别验证。

## 方案选择

| 方案 | 恢复和部署特点 |
| --- | --- |
| 原生 Gateway + IBC | 使用持久 JRE、私有 Xvfb 和同一套 supervisor；可自动恢复已允许的 paper 会话。账户过期或 2FA 由账户持有人处理。 |
| 仅原生 Gateway | 安装相同，登录与重启后的确认依赖人工。 |
| 容器 Gateway | 需要容器引擎、固定镜像与另行管理的持久挂载、端口映射。 |

当前部署采用第一种。Nautilus v1.231.0 的 nautilus-ibapi 10.45.1 是客户端协议库；其与 Gateway 的真实账户兼容性通过盒子上的 paper 验收确认。[固定 Gateway 安装包](https://github.com/gnzsnz/ib-gateway-docker/releases/tag/ibgateway-stable%4010.50.1g) · [IBC 3.24.2](https://github.com/IbcAlpha/IBC/releases/tag/3.24.2)
