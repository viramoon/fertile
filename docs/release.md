# fertile 发版规则（唯一入口）

只有 fertile 对外带版本号；finance / web / frontend 不对外发版。版本文件初始为 0.0.0，发布 tag 为 vX.Y.Z。Maas · Ruler 只按 fertile tag 部署和回滚。

## 准备与固定版本

1. 先合并并推送子模块变更，再在 fertile 发版 PR 更新 gitlink；目标 commit 必须已推送且在各子模块 main 历史中，不能只存在本地。更新指针只能走 PR。
2. 集成检查组合：指针、配置、代码导出的 OpenAPI 快照、生成客户端、静态产物、迁移和恢复方案。当前实际 CI 仅文档检查；子模块与契约检查的实现仍是 TBD。
3. 未来 release 脚本 bump VERSION，读取 gitlink 和上个版本，自动生成 RELEASES.md 条目，列出每个子模块精确 commit 及包含的 PR / issue；不手写发布记录或 deploy/versions.yaml。
4. 在同一发版 PR 中固定版本、生成说明和指针，经检查与评审合并后打 vX.Y.Z；脚本自动给各子模块锁定 commit 打内部 tag fertile-vX.Y.Z 以追溯。tag 必须不可变，重复执行只能接受已存在且指向相同 commit 的 tag。

“更新指针 → 集成 → bump → tag → 自动发布记录与子模块内部 tag”是发布生命周期；生成条目在准备阶段由脚本产出并随发版 PR 固定，正式 tag 指向已包含该条目的提交，不能在 tag 之后手改文件或移动 tag。生成器尚未实现，初始 RELEASES.md 只有说明。

## 部署与回滚

按 tag 获取子模块和对应配置 / 产物，依据 [deploy](../deploy/README.md) 与子模块 RUNBOOK 部署，先本地 live / ready 再公网端到端检查；把 tag、PR / issue、变更影响、验证和回退依据交给 Maas · Ruler。

回滚选已验证的 fertile tag，恢复匹配组件与静态资源；数据库、catalog、节点状态兼容和备份先核对。子模块内部 tag 不能替代系统 tag，agent commit 只追溯知识分发。

## 未来 CI 设计与未定项

check-submodules 校验远端可达和 main 祖先关系；diff-contracts 重导出并比较快照，破坏性变化需迁移方案；设计见 [scripts](../scripts/README.md)。TBD：具体命令、发布身份、权限、首个基线、部分失败可重入策略与集成门禁。初始化不实现这些脚本，不打发布 tag，不实际部署。
