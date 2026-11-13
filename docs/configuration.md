# 配置分层与密钥注入

每个服务采用同一优先级：config/base.yaml < config/<profile>.yaml < 显式环境变量。FERTILE_PROFILE 只接受 cloudbox / stable；未识别 profile 或必要配置缺失时服务必须拒绝启动。部署 profile 与 Nautilus Backtest / Sandbox / Live 独立；paper / 实盘是 Live 的账户选择，stable 不自动打开实盘。当前部署只允许 paper。

| 变量 | 用途与归属 |
| --- | --- |
| FERTILE_PROFILE | Ruler 为每个进程选择部署环境 |
| FERTILE_BOX_HOME | 初始化器的持久前缀，默认 /home/box；测试可覆盖 |
| FERTILE_ROOT | 初始化器生成的 /home/box/fertile 运行根 |
| FERTILE_DATA_ROOT | 服务数据目录，由 Ruler 注入，默认持久根 data |
| DATABASE_URL | web 的 PostgreSQL URL，由 Ruler 从 web.env 注入 |
| IB_GATEWAY_ADDRESS | finance 使用的 Gateway 地址，paper 为 127.0.0.1:4002 |
| TWS_USERNAME / TWS_PASSWORD | Gateway / IBC 所需账户凭据，只注入该进程 |
| WEB_STATIC_ROOT / FINANCE_BASE_URL | web 静态目录与 finance HTTP 地址，后续骨架实现消费 |

配置中只放非敏感值或引用，不把密码写进 YAML、Git、启动参数或日志。具体应用层的加载器由 finance / web / frontend 骨架 issue 实现，本 issue 定义规范与环境引导，不假装应用加载器已经存在。

密钥由 Ruler 在部署时写入 /home/box/fertile/secrets/<组件>.env：目录权限 700，文件权限 600，归对应运行用户；名称例如 finance.env、web.env、gateway.env。Ruler 先检查权限与文件归属，再在进程启动前读取并 export 必要变量。env 文件是受信任的 shell 赋值文件，禁止来源不明内容、命令替换和跨组件混用；不在交互 shell 里全局加载全部密钥，不用 set -x。

env.sh 只包含工具目录与缓存路径，bootstrap 不创建真实账户密码，不修改已有密钥。仓库 .gitignore 忽略 secrets/ 与本地 .env；示例只描述字段，不附真实值。CI 使用临时数据库与 Unix socket peer 认证，不需生产 DATABASE_URL 或账户环境变量。

Postgres 初始只在本机监听：Unix socket peer 认证，TCP 使用 scram-sha-256；Ruler 后续创建最小权限应用角色并注入数据库 URL，应用不可使用 initdb 所建管理角色。轮换密钥后按组件重启并验证健康，数据库凭据轮换与回滚在部署 issue 中演练。
