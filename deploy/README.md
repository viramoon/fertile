# 云盒子部署

[系统清单](service.yaml) 记录部署契约、健康端点和持久路径。[服务配置](services.yaml) 描述进程的启动顺序与就绪条件。Ruler 在 fertile 仓库运行 [操作手册](../ops/ruler-runbook.md) 中的命令。

finance API 和采集器分别在本机的 8081、8082 端口提供健康检查。回测由独立 worker 消费持久队列。web 监听 8090，连接 PostgreSQL，调用 finance API 并托管与自身匹配的 frontend dist。公网入口由现有的 Cloudflare Tunnel 提供。Gateway 只用于已授权的 paper 会话。

/home/box 保存工具、配置、数据、日志、构建和备份。部署锁定三个 gitlink。构建按 fertile 提交写入 releases/，current 指向所选构建。生产发版的统一版本来自 VERSION；本次联调用明确提交验证，未发布新版本。云盒子恢复的步骤见 [bootstrap](bootstrap.md)，守护选型见 [ADR-002](../docs/adr/ADR-002-process-supervision.md)。
