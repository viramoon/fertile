# ADR-002　云盒子的进程守护

状态为已采用，适用于 cloudbox。PID 1 是 tini。只有 `/home/box` 持久，重启后需要重新建立运行进程。普通服务器可选择 systemd 或 supervisord，见 [部署指南](../../deploy/linux-server.md)。

## 选择

使用 supervisord 4.3.0。用 uv 安装在持久工具目录，生成的配置、Unix socket 和 pid 文件存于运行目录。Ruler 使用 scripts/fertile 管理服务，日志按服务轮转。每个服务由守护器直接管理前台进程，退出时向整个进程组发送 TERM，超过宽限后清理进程组。应用的业务就绪由 HTTP、数据时间和 Gateway 协议分别检查。

| 方案 | 在盒子上的影响 |
| --- | --- |
| supervisord | 不依赖 PID 1，提供子进程重启、优先级、socket 控制和大小轮转，Python 与 uv 均已有版本锁定。 |
| s6 | 同样支持无 systemd 的环境，需要另行构建和维护服务目录、日志管线及控制工具。 |
| 自写守护循环 | 需要自行处理孤儿进程、退出竞态、重启退避、轮转和控制接口，测试面积更大。 |

## 恢复边界

supervisord 可以恢复业务子进程。它自身和整机的恢复由 Ruler 执行[重启检查](../../ops/ruler-runbook.md#重启与外部入口)。迁移回退期间按 `migration-rollback.sha` 核对旧发布和隔离目标，再启动服务。启动时先检查配置、可执行文件和外部凭据，保留服务的缺失原因。Gateway 登录预算与应用状态独立于守护重启计数。未来若云盒子提供持久启动钩子，钩子也须执行同一检查。
