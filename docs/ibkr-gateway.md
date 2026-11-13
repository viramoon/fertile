# IBKR Gateway 部署方案（只设计）

本 issue **不安装、不启动、不登录 Gateway**。拟选稳定通道 Gateway **10.50.1g**，配套 IBC **3.24.2**；版本依据为 gnzsnz/ib-gateway-docker 的 stable/Dockerfile，部署时锁定安装包与 SHA256，不使用会漂移的 stable / latest 安装 URL。具体值维护在 deploy/toolchain.env。Nautilus v1.231.0 的 nautilus-ibapi 为 10.45.1，API 包版本与 Gateway 产品版本不是同一个编号；真实协议兼容性要在 paper 验证后才能验收。

## 方案比较与选择

| 方案 | 优点 | 代价与边界 |
| --- | --- | --- |
| 原生 Gateway，手工登录 | 官方流程直观，人工处理 2FA | 重启后需人工操作，自动恢复有限 |
| 原生 Gateway + IBC（拟选） | 安装、JRE、配置与日志可全部放 /home/box，减少对容器守护的依赖 | 要维护 GUI / Xvfb 与 IBC 兼容性，2FA / 周期性重新认证仍需人工 |
| 上游 DockerizedIBGateway + 固定镜像 | Nautilus 已有生命周期管理，环境易复现 | 需可恢复的 Docker daemon、持久 bind mount 和固定 digest；当前云盒子无 systemd，需先解决守护与恢复 |

选择原生 Gateway + IBC 作为盒子方案，保留 Nautilus Docker 方案作为后续部署评估项。IBC 是 UI 自动化，不保证绕过 IBKR 认证或 2FA；登录态过期时停止开新仓并等待人工认证。

## 路径与网络

安装目录 /home/box/fertile/tools/ibgateway/<版本>；自带 JRE 与程序一起持久化。IBC 位于 tools/ibc/<版本>。Jts 设置、受控登录状态和账户配置位于 data/ibgateway，日志位于 logs/ibgateway；实际路径从配置读取。密钥按 [configuration.md](configuration.md) 由 Ruler 注入，登录态属于敏感数据，不进入源码仓库。

paper API 只监听本机 **4002**，不开放实盘 4001，不通过 Tunnel 暴露 Gateway。collector 使用独立 client_id 与只读权限，trader 的写权限单独配置。API 地址从 IB_GATEWAY_ADDRESS 读取。上游 Docker wrapper 默认还会映射宿主 4004；若以后采用容器，必须显式固定端口映射，不能把 4002 与 4004 混为一谈。

## 重启恢复与验收

1. 运行 bootstrap-box.sh 并 source env.sh，恢复系统编译 / GUI 依赖（具体 Xvfb 包与启动命令由部署 issue 落地）。
2. 核对持久安装包版本、hash、权限、配置与登录状态；启动选定 GUI 会话和 IBC / Gateway，不重写密钥。
3. 验证 paper 账户身份、API 4002、行情权限；必要时人工处理 2FA，不无条件循环登录。
4. finance 连接后恢复订阅、按水位补数，拉取账户 / 订单 / 成交 / 持仓快照完成对账，ready 通过才允许开新仓。
5. 记录断线、认证、重连与 ready 时间；执行 ADR-001 的断网、重启和 24～72 小时真实协议观察。当前尚未进行这些账户验证。

常驻守护、GUI 启停、安装包授权获取与恢复命令属于后续部署 issue；本次仅选型与版本方案，没有实际 Gateway 安装或登录副作用。

参考：[上游 stable Dockerfile](https://github.com/gnzsnz/ib-gateway-docker/blob/master/stable/Dockerfile)、[IBC](https://github.com/IbcAlpha/IBC)、[Nautilus Gateway 实现](https://github.com/nautechsystems/nautilus_trader/blob/v1.231.0/nautilus_trader/adapters/interactive_brokers/gateway.py)。
