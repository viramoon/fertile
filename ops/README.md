# 运维入口

Maas · Ruler 在总仓库固定提交下操作。持久根默认为 /home/box/fertile。本机只做本地压力测试，例行巡检查看本机服务状态、依赖和业务端点。公网探测供未来生产服务器显式启用。

- [Ruler 手册](ruler-runbook.md)说明冷启动、生成样例、部署、停止、回滚与重启恢复。
- [健康检查](health-checks.md)列出各服务对应的 HTTP 探测和业务验证。
- [监控与告警](monitoring.md)说明状态迁移、备份年龄、公网与采样记录。
- [备份与恢复](backup.md)给出快照范围、密钥排除、轮转和删库恢复步骤。
- [环境引导](../deploy/bootstrap.md)说明持久化工具链与系统依赖重建。
- [Gateway](../docs/ibkr-gateway.md)说明 paper 登录、只读 API 与人工处理条件。
