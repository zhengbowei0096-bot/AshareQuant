# 代码导航与兼容性

本次按实际职责移动 14 个模块；旧位置保留导入兼容入口。不是两套实现：旧、新导入指向同一个 Python 模块对象。

- `asharequant/database.py` → `asharequant/data_sources/database.py`
- `asharequant/broker_records.py` → `asharequant/data_sources/broker_records.py`
- `asharequant/report.py` → `asharequant/reporting/report.py`
- `asharequant/report_navigation.py` → `asharequant/reporting/report_navigation.py`
- `asharequant/daily_bundle.py` → `asharequant/reporting/daily_bundle.py`
- `asharequant/holding_summary.py` → `asharequant/reporting/holding_summary.py`
- `asharequant/probability_display.py` → `asharequant/reporting/probability_display.py`
- `asharequant/next_session_plan.py` → `asharequant/strategies/next_session_plan.py`
- `asharequant/risk_control.py` → `asharequant/risk/risk_control.py`
- `asharequant/research.py` → `asharequant/backtesting/research.py`
- `asharequant/validation_readiness.py` → `asharequant/evaluation/validation_readiness.py`
- `asharequant/prediction_quality.py` → `asharequant/evaluation/prediction_quality.py`
- `asharequant/connection_health.py` → `asharequant/evaluation/connection_health.py`
- `asharequant/factor_research.py` → `asharequant/evaluation/factor_research.py`

## 为什么部分核心文件仍在包根目录？

模型资格、模拟账户引擎和冻结计划使用源文件指纹。为了不使既有资格和账本失效，本次保留这些核心文件的路径与内容。`models` 提供原函数的统一导入入口，不包装或改变计算。后续如继续迁移，应先设计版本指纹兼容迁移，不能简单移动文件。

- 数据：`data.py`、`history_sources.py`、`public_history.py`、`data_sources/`
- 特征和收益预测：`forecast.py`；七指标：`seven_metrics.py`
- 模型研究：`model_candidates.py`、`risk_models.py`、`lifecycle.py`
- 组合与执行：`decision.py`、`execution_replay.py`、`strategies/`、`risk/`
- 回测：`backtesting/research.py`，严格验证：`strict_validation.py`、`validation_engine.py`
- 模拟：`paper_account.py`（对照）、`research_paper.py`、`active_research.py`、`intraday.py`、`adaptive_intraday.py`
- 调度：`scheduler.py`、`daily.py`；报告：`reporting/`

代码归档及代码变更检查已覆盖嵌套目录。根目录兼容入口暂时保留，避免外部脚本和旧测试失效。
