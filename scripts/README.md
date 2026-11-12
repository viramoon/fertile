# 脚本设计清单

本目录只有设计，不包含任何脚本实现、生成器或构建配置。

## release

输入目标版本、fertile 基线、拟发布 gitlink 与上个版本；检查子模块已推送且属于各自 main 历史，读取 PR / issue 差集；由脚本 bump VERSION 并自动生成 RELEASES.md 条目。条目包含组件、精确 commit、包含的 PR / issue、兼容 / 迁移说明和产物依据。

准备阶段通过发版 PR 固定指针、VERSION 和生成说明；检查通过后把 vX.Y.Z 指向该提交，并自动给每个子模块的锁定 commit 打内部 tag fertile-vX.Y.Z。已存在 tag 必须指向同一 commit，否则停止，不能移动 tag。tag 指向的提交必须已含生成条目，不能打 tag 后手改文档再移动 tag。

TBD：脚本入口、身份权限、PR / issue 归组、初次发布基线、部分打标失败的可重入记录与回退。规则入口见 [release.md](../docs/release.md)。不维护 deploy/versions.yaml，版本依据是 gitlink。

## check-submodules

读取 .gitmodules 与索引 gitlink，校验恰有 finance / web / frontend、URL 与 branch=main 正确且没有 agent。使用新获取的远端对象验证目标 commit 已推送，并检查其为远端 main 的祖先（可为 main 当前头）；不能只检查本地对象存在。检查失败拒绝集成；顺序为先推子模块、再推总仓库。

TBD：凭据最小权限、远端获取、CI 门禁、网络失败分类；本 issue 只写设计，未创建子模块检查 CI。

## diff-contracts

从选定 finance / web commit 用锁定工具导出，规范化后与 contracts 快照比对；记录生成版本、输入和差异，区分兼容与破坏项。禁止以手改快照消除差异。生产者 / 消费者更新顺序见 [contracts](../contracts/README.md)。

TBD：命令、规范化工具、破坏性差异门禁与例外记录流程；本次不生成快照或实现比对 CI。
