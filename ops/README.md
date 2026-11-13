# 运维入口

由 Maas · Ruler 依据 fertile tag 与 [deploy](../deploy/README.md) 执行部署 / 回滚，读取子模块 RUNBOOK，不按各分支临时部署。

- [health-checks.md](health-checks.md)：本地 live / ready 与公网端到端检查。
- [monitoring.md](monitoring.md)：连接、恢复、数据新鲜度与错误观测。
- [ruler-runbook.md](ruler-runbook.md)：发布交接、暂停、故障记录与回滚。
- [bootstrap](../deploy/bootstrap.md)：云电脑重启重建。

TBD：命令、监控工具、通知通道与值班责任，在后续运维 issue 确定。本目录不含运行脚本，文档中的检查尚未实现。
