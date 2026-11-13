# 服务部署唯一准绳

fertile 的 deploy/ 是系统服务部署的唯一来源。[service.yaml](service.yaml) 聚合组件清单并定义部署形态；finance、web 的 service.yaml 提供组件字段，RUNBOOK 提供操作设计。agent deploy-map 只做 skill / prompt / 工作流分发，不作为服务拓扑或版本依据。

当前只有 finance 的 trader / collector / api 与 web 常驻业务进程，frontend Vite 产物由 web 托管；backtest 是任务，agent 是独立知识仓库。Gateway、PostgreSQL 与 Tunnel 为基础依赖，实际实例与启动命令 TBD。

按只有 /home/box 持久处理。代码、配置、数据、日志、二进制与备份均使用配置化持久目录；/etc、/srv、/usr 下系统级改动必须写进未来可重复执行的初始化流程，不能靠一次手工设置。重启重建设计见 [bootstrap.md](bootstrap.md)。

所有部署 / 回滚以 fertile tag 锁定的指针、配置和产物为单位；不维护手写 versions.yaml，gitlink 是子模块版本依据。环境通过 config/base.yaml + cloudbox.yaml 或 stable.yaml 区分，运行模式和账户独立，当前只用 paper。

环境初始化已由 [bootstrap-box.sh](../scripts/bootstrap-box.sh) 实现，Postgres 命令与重建步骤见 [bootstrap.md](bootstrap.md)。业务配置加载器、服务端口 / 域名与常驻守护由后续骨架 / 部署 issue 实现；聚合 service.yaml 仍是设计清单。
