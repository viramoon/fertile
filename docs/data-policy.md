# 四仓数据发布政策

fertile v1 面向单人私用。付费数据、真实行情及其脱敏副本、运行截图和录屏、数据库、日志导出、崩溃转储、备份与凭据一律留在受控环境。约束覆盖四仓 Git 历史、文档、Git LFS、issue/PR 正文和评论、附件及 Actions artifact。测试使用可复现的合成输入或由合成输入生成的回放，全程离线。真实 IBKR 验收在独立私有部署中执行，限 paper 只读行情。v1 无下单入口。

## 许可与审批记录

规则文件使用 UTF-8 JSON，顶层 `schema_version` 为整数 `1`。许可和审批各有一份同版本记录，以相同的小写连字符产品 ID 关联。同一产品的某项用途须在取得该用途的授权后才能启用，私用、缴费、加密备份均不能推出授权。[规则文件](../policy/data-guard.json)

| 记录 | v1 字段与约束 |
| --- | --- |
| [市场数据许可](../policy/market-data-licenses.json) | `products` 对象。每个产品包含 `display_live`、`record_local`、`derive_non_display`、`backup_personal_drive`。值为 `true`（已确认允许）、`false`（已确认禁止）或 `null`（待确认，按禁止处理）。`redistribute` 恒为 `false`。`basis` 为不含查询参数的公开 HTTPS 条款页，或 `pending`。填写任何用途结论时须同时注明公开依据。 |
| [能力与价格审批](../policy/market-data-approvals.json) | `approvals` 对象。每条记录仅含 `capability`（`pending`、`approved` 或 `declined`）和 `price`（未知为 `null`）。已知价格采用 `{"amount":"0.1250","currency":"USD","period":"month"}` 形式。金额为非负十进制字符串，最多八位整数、六位小数。周期取 `month`、`year` 或 `one-time`。 |

许可和审批记录只公开产品能力、公开依据与价格。账户、支付凭证、持仓、请求响应、实际样本、收据和截图由授权人员私下保管。审批状态供评审记录使用，实际启用按产品与用途核权。初始条目待确认，TG 未连接服务验证权限或购买订阅。

## 合成来源与规则格式

总仓用清单的 `fixtures` 对象登记 `fixture_root` 下的数据 fixture，用 `documents` 对象登记经审查的合成 Markdown 文档。两者都用仓库相对路径作键，每条记录只接受表中的四个字段。同一路径只能登记一次。v1 旧清单省略 `documents` 时按空白名单处理。[合成来源清单](../policy/synthetic-fixtures.json)

| 字段 | 含义 |
| --- | --- |
| `source` | 固定为 `synthetic`。 |
| `generator` | 同一索引或提交中的普通源文件，相对路径，后缀见 `generator_extensions`。 |
| `seed` | 可复现的输入种子或确定性模板标识，1–64 位字母、数字、点、下划线或连字符，首位字母或数字。 |
| `sha256` | 生成文件完整字节的 64 位小写十六进制 SHA-256。 |

路径区分大小写，使用 `/`，禁止绝对路径、空段、点段、反斜线和控制字符。先提交生成器、种子及目标路径和摘要，说明离线生成命令，供评审核对可复现性。此时目标文件可以尚未纳入 Git。登记合入目标分支后，将批准提交纳入内容分支，再提交摘要匹配的生成文件。现有路径更换摘要也需要先走独立审批。生成器只接受合成输入，钩子校验记录和摘要，生成命令由评审离线复现。测试内按固定输入即时生成的临时文件以测试源码记录生成逻辑和种子。

各仓接入时对齐以下规则语义。版本变化须与下游协调。

| 规则字段 | v1 约定 |
| --- | --- |
| `max_blob_bytes`、`max_pr_bytes` | 从 Git 对象读取大小后再扫描内容。总仓每个 blob 上限 1 MiB，PR 标题和正文合计以及每条 Git 提交或 tag 消息上限 64 KiB。 |
| `blocked_directory_pattern`、`blocked_filenames`、`blocked_extensions` | 对路径组件小写匹配，覆盖数据、catalog、截图、日志、导出、备份、转储与秘密目录，以及 LFS 配置、常见私有文件名与不透明格式。 |
| `fixture_root`、`fixture_manifest`、`fixture_extensions`、`generator_extensions` | 选择合成来源清单、允许的 fixture 文本类型和生成器源文件类型。CSV、TSV、JSONL、NDJSON 以及 fixture 目录内的 JSON 必须登记。fixture 登记只允许携带合成行情形状，其他限制仍有效。 |

规则文件必须包含上述全部字段与整数版本 `1`。大小上限为正整数，路径为仓库相对路径，fixture 根以斜线结尾，目录模式为可编译且不匹配空组件的正则表达式。后缀与文件名列表必须是非空且无重复项的数组，使用小写字符串。缺字段、未知字段、错误类型、非法值或不支持的版本都会使检查失败。目录正则编译失败统一报告 `invalid-rule-file`。

Markdown 表格和 HTML 表格依据列组合识别行情，支持中英文的标的、时间、价格、数量、成交、买卖报价和档位。大小写、空格、连字符、下划线与常见档位编号采用统一匹配。Markdown 引用中的表格和 CSV 也会被扫描。行情形状及嵌图默认拒绝，嵌图检查覆盖 Markdown 行内、完整引用、折叠引用、快捷引用和指向图片的引用定义，也覆盖 HTML 图像标签与内联 data URI。

若需发布合成行情表或合成示意图，将完整的 `.md` 或 `.markdown` 文件按路径、SHA-256 和合成来源登记到 `documents`。图像例外限于 Markdown 行内 Base64 PNG，支持无附加块、非交错的 8 位灰度、RGB 及其带 alpha 格式。检查块边界、顺序、CRC、压缩流和扫描行，要求 `IEND` 是末块，解压后的扫描行总大小上限为 16 MiB。图像字节随文档摘要锁定。外部或单独引用的图片、通用 data URI、凭据、日志、LFS 和不透明二进制仍适用原检查。白名单只适用于登记的 Git 文件，PR 正文和 Git 消息按默认规则扫描。

总仓还检查 UTF-8 文本、文件签名、常见 JSON 行情记录、日志或 SQL 转储、文件名和正文中的常见凭据、GitHub 附件链接、LFS 指针与 `.gitattributes` 中的 LFS 规则。扫描是特征匹配，评审仍须确认来源与内容。诊断输出使用规则代码、输入类别或 Git 对象 ID，原始路径和匹配内容仅供本地核对。[总仓检查器](../scripts/data-guard.py)

## 总仓接入

每个开发克隆在首次提交或推送前启用版本化钩子。已有自定义 hooks 的克隆应先执行数据检查，再执行其他钩子。四仓禁用 LFS 跟踪和上传钩子。

~~~sh
git config --local core.hooksPath .githooks
python3 -B scripts/data-guard.py index
python3 -B tests/data_policy_test.py -v
~~~

`pre-commit` 从索引读取规则、文件及声明，从 `HEAD` 读取已批准的来源记录。候选记录必须与可信记录一致，文件字节必须匹配批准的 SHA-256。没有 `HEAD` 或可信版本的规则和来源记录无效时，白名单为空。`pre-push` 使用推送 tip 与本地 `refs/remotes/<remote>/main` 的唯一合并基作为可信版本。缺少远端跟踪引用或唯一合并基时白名单为空。远端目标引用的旧提交只用于确定检查范围。检查覆盖 tip 和相对于远端旧提交及已知合并基新增的提交，并校验各提交中已有的规则文件。拒绝浅克隆。Gitlink 仅包含提交引用，由子仓在自己的 Git 图中检查实际内容。

CI 使用 PR 基准与合并候选的唯一合并基批准白名单，整段候选历史共用这份批准记录。合并候选中的规则用于检查 PR 基准之后的提交、候选树和 PR 标题及正文。PR 编辑会重跑检查。首次上传的防线是本地钩子和提交人审查。GitHub 网页上的上传、issue/评论、外部附件及绕过本地钩子的操作需要人工管控，因为 CI 在 PR 收到后才运行。管理员可把数据政策和文档检查设为合并必需检查，并在仓库支持时配置服务端文件限制。遇到误提交应立即停止推送。如果已上传，通知仓库管理员处理历史与附件，公共说明只写能力与处理结果。[数据政策 CI](../.github/workflows/data-policy.yml)

### CI 制品

总仓只上传通过检查后生成的两种报告。`bootstrap-timing.json` 包含检查名、成功状态和两次安装耗时的整数秒数。`supervisor-drill.json` 包含检查名和成功状态。两者均有整数 `schema_version`。工作流在成功的检查之后运行 `data-guard.py artifact`，通过时生成单个固定路径文件，下一步仅上传该文件。bootstrap 安装测试和 supervisor 演练的 stdout、stderr 重定向到 runner 上仅属主可读写的日志文件，测试失败会使对应步骤失败。文档 CI 核对每个直接上传步骤的名称、路径和前置报告命令，并以合成命令复测日志隔离。新增上传入口须同步更新上传范围、报告字段与测试，评审同时覆盖间接上传和手动 PR 附件。

## 私有目录

部署的持久根、独立的行情和研究 catalog、数据库目录、导出、截图、日志及备份一律在四仓工作树外。测试使用工作树外的临时目录。运维先选择专用绝对路径，以受限权限保存，并对每个独立克隆执行下列校验。Git linked worktree 和符号链接解析后的实际路径均纳入比较。

~~~sh
python3 -B scripts/data-guard.py private-root \
  /srv/fertile-private /srv/fertile-review-output
~~~

请传实际使用的根和显式覆盖的单独目录。此命令校验路径关系，不读取配置、凭据或目录内容。实际目录与现有部署的关系见 [部署清单](../deploy/service.yaml) 和 [Ruler 手册](../ops/ruler-runbook.md)。

## 四仓接入范围

| 仓库 | 接入内容 |
| --- | --- |
| fertile · TG | 维护共享 v1 规则和记录格式。总仓启用索引、push 引用、PR Git 历史与文字、CI 报告门禁。 |
| finance · [FG](https://github.com/viramoon/finance/issues/31) | 在自身仓内接入相同检查。为合成事件/回放 fixture 登记来源并校验。行情采集、WAL、catalog、授权材料和数据导出留在工作树外，记录与派生按产品用途核权。 |
| web · WG | 在自身仓内接入相同检查。请求与异常体、审计日志、数据库导出、测试报告和生成客户端样例覆盖凭据及行情正文。保留正常生成的 OpenAPI 快照与客户端验证。 |
| frontend · [UG](https://github.com/viramoon/frontend/issues/16) | 在自身仓内接入相同检查。以可追溯的合成行情驱动视觉测试，将截图、录屏、trace 和报告输出放到工作树外，发布制品只含明确许可的合成摘要。 |

FG、WG、UG 各自交付钩子和 CI，使用同版规则格式及自仓文件规模、生成物与合成来源配置。各仓配置自己的受保护分支与必需检查。OpenAPI、生成客户端和流协议继续按 [契约文档](../contracts/README.md) 交接。TG 的规则、记录和报告属于开发与评审输入。
