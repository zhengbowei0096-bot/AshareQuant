# AshareQuant 项目维护规则

## 用户长期要求：代码、配置、文档和文件导航同步维护

每次修改本项目，都必须在同一次任务中检查并更新受影响的说明，不能把文档维护留到以后。没有实际变化的内容不做无意义改写。

### 变更与同步范围

- 新增、删除、移动、重命名文件或改变模块职责：同步 `docs/文件逐项说明.md`、对应 HTML 说明、`00_程序文件导航/` 的分类、编号、链接和各组用途说明；保持说明与导航同序。
- 功能、安装、依赖、运行入口或目录变化：同步 `README.md`、架构说明及相关使用示例。
- 配置项、含义或默认值变化：同步非敏感配置模板及参数说明；不得未经授权调整真实账户配置或风险参数。
- 预测、交易计划、模拟账户、指标口径或风险规则变化：同步相关输出解释、报告生成模板及必要测试。只刷新生成报告不算完成模板更新。
- 每日或盘中调度变化：同步运行时间、时区、前提条件、故障行为及运维说明。
- 测试、部署或回滚方式变化：同步验证与恢复说明，标明测试日期/范围，不把历史通过结果冒充本次验证。

### 交付检查

1. 确认每个相关源文件都有准确说明；兼容入口要写明实际实现位置。
2. 检查导航目标、相对链接、命令及配置名称仍然有效；新增/删除文件不得留下遗漏或失效链接。
3. 验证受影响的测试或流程，明确没有验证的部分。
4. 在交付中简要说明哪些文档、导航和模板已同步；如无需变更，说明已检查且无受影响内容。
5. 桌面运行项目与源码发布目录分开管理。应用到哪个目录必须核实；本地改好不能宣称 GitHub 已更新。发布后核对远程提交。

### 保留约束

- 不因整理说明而修改算法、预测值、策略参数、风险门槛或冻结计划。
- 有源码指纹绑定的文件不得随意重命名、移动；优先更新导航链接。
- 已冻结预测、成交、收益账本及历史归档保留原始证据，不追改成新的规则。需要更正时追加注明原因的更正记录。
- 私人持仓、账户、凭据、日志、报告和本机绝对路径不得进入公开源码说明或提交。
- 文档说明以实际代码与验证结果为依据，不把研究功能描述为正式验证通过，不把模拟交易描述为真实下单。

这是一项每次修改时必须执行的维护要求，不是已经部署的自动文档生成服务。


## Workspace organization (英文分类工作区)

Future file changes must update `config/workspace_layout.json`, `docs/FILE_GUIDE.md`/`.html` and the compatibility file guide, then refresh with `scripts/organize_workspace.py`. Keep the ten numbered category names English-first with Chinese notes. The older `00_程序文件导航/` entry remains a compatibility route to the current source categories. Do not move fingerprint-bound source/configuration files or ledger files solely to tidy Finder; keep the runtime paths and classify them with relative links. New unclassified files must be reviewed before hiding them. (新增文件须同步分类、用途与导航；不得为整理而破坏指纹或账本路径。)

Public GitHub navigation uses the same ten category folders, with only each category README committed. When adding public files, update `public_navigation.files` and category coverage in `config/workspace_layout.json`, then regenerate the category pages and file guides together. Do not publish generated local launchers, symlinks, inventories, accounts or reports. (公开分类页与桌面同序；新源码须加入审核清单并归类，私有运行产物不发布。)

Keep `root_visible_files` aligned with homepage documentation. `README.md`, `requirements.txt` and `AGENTS.md` remain directly visible beside the numbered categories; do not hide these entry files on future refreshes. (首页文件与分类并列可见，后续整理不得再次隐藏。)

Keep data comparison and historical acceptance utilities in `tools/`, and recorded dependency environments in `config/environments/`. Root `requirements.txt` remains the basic installer list. Do not reintroduce removed forwarding launchers; use the CLI and document any path migration and source-archive coverage. (工具与环境记录归类保存，首页仅留常用入口。)
