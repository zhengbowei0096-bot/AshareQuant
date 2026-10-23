# 本次整理验收

本次未调整预测算法、风险阈值和交易规则。与旧 Git 快照相比的差异还包含此前已在本地运行的功能；本次整理的行为基准是整理前的实际运行项目。

| 检查 | 结果 |
|---|---|
| 整理前原有测试 | 374 通过 |
| 整理后与实际部署后 | 各 377 通过（新增导入兼容、初始化不覆盖、嵌套归档检查） |
| 模块导入 | 75 个模块可导入 |
| CLI | `python -m asharequant.cli --help` 成功 |
| 14 个移动模块 | AST 比较一致，仅相对导入层级变化 |
| 39 个未迁移核心文件 | 与运行基准逐字节一致 |
| 真实历史缓存复算 | 相同特征、10 条预测、拟合参数完全一致 |
| 模型/风险模型/模拟引擎指纹 | 前后完全一致 |
| 本地配置及调度配置 | 保持不变 |
| 文档链接与 Git 空白检查 | 通过 |

没有把研究模型重新注册为正式模型，没有重新结算账本，没有执行真实下单。本次没有完整联网重跑每日流水线或在开盘期间验证成交，也没有完成全新机器安装验收。现有测试包含时间序列泄漏、交易费用、模拟执行、调度与报告路径检查，但不代表穷尽所有错误。

## 维护与恢复

部署前对所有替换文件及缓存建立 `backups/organization-时间/before.tar.gz`，同目录 `manifest.json` 记录替换、新增和缓存删除清单。回滚时应先暂停本地服务，在备份恢复前检查归档路径；恢复 replaced 文件，移除本次 added 文件，再恢复服务和运行测试。不要覆盖随后发生变化的数据、配置和账本。缓存可由 Python 自动重建。

可生成的缓存与有价值的研究产物分开处理：本次只清理 Python 字节码及系统缓存；数据、日志、报告、研究记录、历史备份未删除。没有证据认定无用的旧脚本保留。

尚存技术债务：源文件指纹与物理目录耦合；部分模块仍较大；券商导入适配限定华福；根目录兼容入口暂时存在；跨平台安装及完整环境锁定需进一步验收。未指定项目许可证，需要权利人作出选择。


报告保留规则变更（2026-10-21 北京时间）：报告导出相关10项测试通过，包含跨午夜选取、备份内容、重复清理、跨日期隔离及不完整导出保护。未改动预测或模拟账本算法。旧桌面副本可按 backups/desktop_report_versions 内恢复清单移回原日期目录。

## 2026-10-21 行情驱动执行升级

本次新增测试覆盖后续报价撮合、部分成交累计佣金、无新增成交量不撮合、限价、T+1、断线/撤单、冷却、幂等、风险退出、PnL恒等式、SQLite重启和收盘一次结算、保存系数推理与未来数据截断。合成数据仅用于测试。

原30只股票90组已发布预测用原参数重放：最大数值差0；该检查不等于盘中效果通过。闭市公开行情连接检查30/30腾讯、30/30新浪返回，本次请求约1.35秒，返回上一交易日报价，不能据此交易；供应商实际更新频率及端到端交易时段延迟尚待首个盘中验证。无券商连接测试或真实下单测试。

本次完整回归：源码目录396项通过（4.231秒），桌面运行目录396项通过（3.842秒）。最终导航链接修复另执行4项导航测试通过。78个子模块导入、94个导航目标和导出报告相对链接检查通过。浏览器file协议被安全策略禁止，未完成浏览器截图验收。


## 无声 Mac 弹窗提醒
独立 `asharequant/mac_alerts.py` 每30秒读取动态模拟提醒与健康状态，不调用语言模型，不修改账本。仅服务异常及恢复触发无声对话框；模拟买入、卖出、成交、撤单/拒单不弹窗，仍保留报告与账本记录。委托与成交分别显示；同订单更新合并，超过5分钟注明历史。重复事件按ID抑制，连续两次研究推理异常才提醒；交易时段健康超过120秒未更新提醒一次，恢复再提醒一次。
对话框120秒后自动关闭；电脑须开机、用户登录且桌面可用，离线或休眠不能保证及时提醒。投递状态独立保存在 `logs/mac_alerts/`，失败重试；崩溃后可能重复提醒，不重复交易。系统接受弹窗不等于用户已经阅读。
启动：`python -m asharequant.mac_alerts --root .`。服务为 `com.asharequant.mac-alerts`，独立于行情、模型和18点每日分析；停用此服务仅停止本地弹窗。聊天轮询仅在弹窗人工确认后暂停，18点任务保留。
验证（2026-10-21）：7项通知逻辑测试通过，涵盖去重、部分成交数量、历史卖出、异常/恢复、持续异常、过期健康和无声调用；用户确认无声对话框可见。尚未用真实盘中买卖事件验收。回滚：停止本地提醒服务并恢复聊天轮询，交易算法及账本无须恢复。

2026-10-21 用户更新：关闭所有交易事件弹窗，仅保留异常及恢复提醒；聊天轮询保持暂停，18点分析不变。交易事件仍标记已处理，避免旧消息补发。


2026-10-23：单一主账户展示验证。报告中文阅读层10项、每日导出及收益读取15项、导航4项测试通过。覆盖只读读取已冻结主账本、哈希篡改拒绝、日期截断、旧券商记录不混入、无账本不创建、未结算不填零、旧空表引用本次归档、金额与百分比展示不改原值、原始文件不改写、链接目标及最终导出哈希。验证仅针对报告和数据读取，不代表重新验证预测模型或盘中成交。
回滚：恢复变更前报告模块和文档副本；旧归档只恢复展示 HTML，不回滚或重写账户账本、预测及成交。


2026-10-23 workspace organization: 5 isolated maintenance tests passed (read-only input preservation, link validity, safe cache removal and restore, repeatability, refusal of unclassified/edited files, and path traversal rejection). Runtime import and path checks are recorded separately in the local organization verification record; this is not a new prediction or trading qualification.

2026-10-23 runtime workspace checks: 54 related tests passed; all 81 Python modules imported; all 283 category links and source-guide targets resolved; six installed service configurations retained valid paths. The command entry and two report-launcher shell scripts passed checks. Core model/execution files and local trading configuration contents were unchanged. Finder hides the compatibility root entries; ten categorized folders remain visible. These checks do not revalidate live market inputs or strategy quality.

## 2026-10-23 Source publication (源码同步验收)

- 保留远程已有的 GuanlanQuant 项目展示名与仓库链接，运行包继续为 `asharequant`。
- 完整自动化测试：425 项通过。81 个 Python 模块导入成功，3 个命令帮助入口通过。发布源码与桌面运行项目的 81 个模块逐字节一致，预测、风控和执行算法未因发布修改。
- 344 个本地文档链接解析通过；Markdown 文件逐项说明可在 GitHub 直接点入源码，HTML 说明供本地浏览器使用。
- 干净源码副本初始化生成 13 个本地配置/观察池文件，重复初始化不覆盖；动态服务默认关闭，配置文件名已统一为 `dynamic_config.json`。可生成十类工作区、116 个有效链接，未创建账户数据库或报告。
- 测试中的账户格式样例全部使用虚构数值。检查发布清单及待发布提交历史中的密钥、本机路径、账户标识、已知私人值和本地运行产物；安全检查不代表能识别任意未知秘密。
- 本次没有联网重跑行情/每日研究，没有改写账户账本，没有新做盘中成交或全新环境依赖安装验收。

恢复：使用发布前源码备份或 Git 中的上一版本恢复文档、模板和工具；工作区分类使用 `organize_workspace.py --rollback`。不得把代码回滚当成账户回滚，不覆盖冻结成交和收益。新生成配置名修正只影响新安装，已有私人配置内容不变。
