# 配置与密钥

应用按 config/base.yaml、config/<profile>.yaml、显式环境变量顺序加载，后者覆盖前者。部署 profile 为 cloudbox 或 stable。Nautilus 的回测 / 沙盒 / Live 是另一个维度；当前 Gateway 仅接受 paper 账户。frontend 只访问同源 web API。

## 持久运行配置

Ruler 在运行根 config/runtime.env 中填写每行一个非敏感 KEY=VALUE。主管按允许的变量列表加载；显式传给控制命令的同名变量优先。每次启动、恢复、巡检需要一致的设置，长期配置请保存在文件中。

| 参数 | 生效位置 |
| --- | --- |
| FERTILE_BOX_HOME、FERTILE_ROOT | bootstrap 默认使用 /home/box，生成的 env.sh 设置运行根 /home/box/fertile。 |
| FERTILE_MARKET_SOURCE | 默认 ib。测试使用 replay，独立存入 data/replay。生产源与回放源切换需核对 catalog 与队列目录。 |
| FERTILE_PROFILE、FERTILE_DATA_ROOT | 选择服务配置和持久数据目录。显式挂载必须存在。 |
| FINANCE_API_PORT、FINANCE_COLLECTOR_PORT、WEB_PORT | 默认在本机监听 8081、8082、8080。 |
| FINANCE_COLLECTOR_CONFIG、IB_GATEWAY_ADDRESS | 指向当前行情源的配置和本机 paper 接口 127.0.0.1:4002。 |
| FERTILE_HEALTH_CONFIG、FERTILE_PUBLIC_ORIGIN | 站点巡检配置和经确认的 HTTPS 入口。公网入口为空时采样会记录跳过。 |
| FERTILE_BACKUP_DAYS、FERTILE_BACKUP_WEEKS、FERTILE_BACKUP_REMOTE | 本地保留周期与可选 rclone remote 名。详细设置见 [备份](../ops/backup.md)。 |

主管为 web 注入同一 release 的 WEB_STATIC_ROOT、finance HTTP 地址和 PostgreSQL socket。仅在需要外部或独立的数据库身份时设置 web-database.env。迁移、权限和停库恢复按 [Ruler 手册](../ops/ruler-runbook.md) 操作。

## 凭据与轮换

| 文件 | 使用者与内容 |
| --- | --- |
| secrets/web.env | 仅 web 管理接口和授权的运维冒烟读取。文件中只有 WEB_ADMIN_TOKEN。 |
| secrets/web-database.env | 可选，web 专用的 DATABASE_URL。 |
| /home/box/fertile/secrets/ibkr-paper.env | 仅 Gateway 的 IBC 包装器读取 IBKR_USERNAME、IBKR_PASSWORD。健康检查只输出服务器时间和 DU 账户前缀。 |
| secrets/alerts.env | 可选 ALERT_WEBHOOK_URL。默认告警写本机 JSONL。 |
| /home/box/.cloudflared/ | cloudflared 使用既有 Tunnel 文件。公网域名另由用户确认。 |

创建 secrets 目录时使用 0700，凭据文件为服务属主的 0600 普通文件。运行时 env.sh 只提供工具链路径和缓存配置。备份排除凭据，Git 忽略 secrets/ 和本地 .env 文件。轮换时在盒子本地替换对应文件并重启相应服务，保留脱敏后的就绪和业务验证结果。详情见 [Gateway](../ops/ibgateway.md)、[监控](../ops/monitoring.md) 和 [管理令牌](../ops/ruler-runbook.md)。
