# Maas · Ruler 操作设计

只按 fertile tag 部署和回滚，服务依据是本仓库 [deploy](../deploy/README.md)；读取 [finance RUNBOOK](https://github.com/viramoon/finance/blob/main/RUNBOOK.md)、[web RUNBOOK](https://github.com/viramoon/web/blob/main/RUNBOOK.md)、[frontend RUNBOOK](https://github.com/viramoon/frontend/blob/main/RUNBOOK.md)。agent 的分发映射不部署业务服务。

## 发布与回滚交接

接收 tag、集成 PR、子模块 PR / issue、配置 / 迁移影响、构建摘要、验证结果和已验证回退 tag。核对 tag 与 gitlink，拒绝未推送或不在子模块 main 历史中的目标；依赖、备份和启动步骤见 deploy/bootstrap.md。

按各 RUNBOOK 部署后，先检查本地 live / ready，再从公网域名检查页面、资源与 API，保留证据。回滚恢复匹配的组件和静态产物，数据库 / catalog / 节点状态兼容需先确认；不能只换二进制。

## 故障处置

行情陈旧、状态不可靠或写响应丢失时先暂停开新仓，核对订单与成交再决定撤单或重试，不无条件重放写入。web / Tunnel 故障优先恢复入口，不重启健康交易核心。云电脑重启按 bootstrap 重建非持久系统配置。

现网问题开 production-bug issue，附复现、期望 / 实际、关联原 issue、fertile tag、profile、时间、脱敏证据和需补测试；开发中问题原分支修复。恢复后回写仓库当前事实，重大决策新增 ADR。

TBD：具体命令、操作入口、凭据引用、通知通道、负责人和恢复阈值，由运维实现 issue 确定；本次未接入机器人或发送通知。
