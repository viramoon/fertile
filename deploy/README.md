# 部署

[系统清单](service.yaml) 记录部署契约、健康端点和持久路径。[服务配置](services.yaml) 描述进程的启动顺序与就绪条件。[普通服务器部署指南](linux-server.md) 包含 Ubuntu 24.04 和 Debian 12 的构建、systemd、supervisord 与 HTTPS 反代步骤。Ruler 按[操作手册](../ops/ruler-runbook.md)管理本地压测盒子。

finance API 和采集器分别在本机环回的 8081、8082 端口提供健康检查。回测由独立 worker 消费持久队列。web 默认监听 `127.0.0.1:8090`，连接 PostgreSQL，调用 finance API 并托管同版本的 frontend dist。普通服务器由运维显式配置其 HTTPS 入口及公网检查。Gateway 只用于已授权的 paper 会话。

上述文件和工具保存在部署根内，`stable` 默认使用 `/var/lib/fertile`，`cloudbox` 默认使用 `/home/box/fertile`。环境变量 `FERTILE_ROOT`、`FERTILE_BOX_HOME` 可覆盖默认根。部署锁定三个 gitlink，按 fertile 提交写入 `releases/`，`current` 指向所选构建。生产发版的统一版本来自 VERSION。工具引导和云盒子恢复见 [bootstrap](bootstrap.md)，云盒子的守护选型见 [ADR-002](../docs/adr/ADR-002-process-supervision.md)。
