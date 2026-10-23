# 01_Reports (报告与收益)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Read the main account, predictions and validation. Archived snapshots keep their original dates. (查看主账户、预测、验证及按日期保存的历史报告。)

Report generation is included; actual reports and returns are generated locally after a valid run. (包含报告功能，实际报告与收益在本机有效运行后生成。)

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [asharequant/reporting/daily_bundle.py](../asharequant/reporting/daily_bundle.py) | Reporting module: reporting/daily_bundle.py (只读主动态账本核算已结算收益，按日期汇总预测与每日报告，默认只展示当前策略；同日只留最新报告，旧版本移至备份。) |
| [asharequant/reporting/plain_chinese.py](../asharequant/reporting/plain_chinese.py) | Reporting module: reporting/plain_chinese.py (报告中文阅读层：解释状态、生成附件阅读表格，将旧空收益入口改为同一归档的动态主账户结算，说明缺项；检查本地链接。) |
| [asharequant/reporting/report_navigation.py](../asharequant/reporting/report_navigation.py) | Reporting module: reporting/report_navigation.py (管理完整总览与动态模拟入口，每30秒同步桌面实时展示及附件，隐藏旧策略；不修改模型或账本。) |
| [asharequant/reporting/report.py](../asharequant/reporting/report.py) | Reporting module: reporting/report.py (生成主要分析网页、图表和指标展示。) |
| [docs/DYNAMIC_EXECUTION.md](../docs/DYNAMIC_EXECUTION.md) | Dynamic paper execution, prerequisites and limitations. (动态模拟执行、运行前提与限制。) |
