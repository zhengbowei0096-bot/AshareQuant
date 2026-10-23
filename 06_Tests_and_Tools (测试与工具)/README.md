# 06_Tests_and_Tools (测试与工具)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Automated tests and explicit maintenance tools; verification scripts can have side effects. (自动测试与维护工具，验收脚本可能联网或写记录。)

Public source and documentation only. Local runtime records remain on your computer. (这里只发布源码与说明，本机运行记录不上传。)

## Files (文件)

| File (文件) | Purpose (用途) |
|---|---|
| [tests/test_active_research.py](../tests/test_active_research.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_adaptive.py](../tests/test_adaptive.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_connections.py](../tests/test_connections.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_connectivity.py](../tests/test_connectivity.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_corporate_ledger.py](../tests/test_corporate_ledger.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_daily_acceptance.py](../tests/test_daily_acceptance.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_daily_bundle.py](../tests/test_daily_bundle.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_database.py](../tests/test_database.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_decision.py](../tests/test_decision.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_dynamic_execution.py](../tests/test_dynamic_execution.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_factor_research.py](../tests/test_factor_research.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_factors.py](../tests/test_factors.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_forecast.py](../tests/test_forecast.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_history_retries.py](../tests/test_history_retries.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_history_sources.py](../tests/test_history_sources.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_holding_summary.py](../tests/test_holding_summary.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_indices.py](../tests/test_indices.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_intraday.py](../tests/test_intraday.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_iteration.py](../tests/test_iteration.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_iteration_null_candidate.py](../tests/test_iteration_null_candidate.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_layout.py](../tests/test_layout.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_lifecycle.py](../tests/test_lifecycle.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_mac_alerts.py](../tests/test_mac_alerts.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_model_candidates.py](../tests/test_model_candidates.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_model_monitor.py](../tests/test_model_monitor.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_next_session_plan.py](../tests/test_next_session_plan.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_paper_account.py](../tests/test_paper_account.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_plain_chinese.py](../tests/test_plain_chinese.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_prediction_quality.py](../tests/test_prediction_quality.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_real_fees.py](../tests/test_real_fees.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_report_navigation.py](../tests/test_report_navigation.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_research_paper.py](../tests/test_research_paper.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_research_policy.py](../tests/test_research_policy.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_risk_control.py](../tests/test_risk_control.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_risk_models.py](../tests/test_risk_models.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_seven_metrics.py](../tests/test_seven_metrics.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_strategy_experiment.py](../tests/test_strategy_experiment.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_strict_validation.py](../tests/test_strict_validation.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_system.py](../tests/test_system.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_work_archive.py](../tests/test_work_archive.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [tests/test_workspace_organization.py](../tests/test_workspace_organization.py) | Regression and integrity checks, run with unittest. (回归与完整性检查。) |
| [scripts/init_config.py](../scripts/init_config.py) | Configuration initialization and workspace organization. (配置初始化与工作区整理。) |
| [scripts/organize_workspace.py](../scripts/organize_workspace.py) | Configuration initialization and workspace organization. (配置初始化与工作区整理。) |
| [tools/decision_example.py](../tools/decision_example.py) | Existing data verification and decision examples. (已有数据验证工具与决策示例。) |
| [tools/verify_stage1.py](../tools/verify_stage1.py) | Existing data verification and decision examples. (已有数据验证工具与决策示例。) |

## Root_Entry_Points (兼容与验收入口)

| File (文件) | Purpose (用途) |
|---|---|
| [check_data.py](../check_data.py) | Compatibility or validation entry: check_data.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [compare_sources.py](../compare_sources.py) | Compatibility or validation entry: compare_sources.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [market_data.py](../market_data.py) | Compatibility or validation entry: market_data.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [market_data_tx.py](../market_data_tx.py) | Compatibility or validation entry: market_data_tx.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [update_watchlist.py](../update_watchlist.py) | Compatibility or validation entry: update_watchlist.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [verify_factors.py](../verify_factors.py) | Compatibility or validation entry: verify_factors.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [verify_models.py](../verify_models.py) | Compatibility or validation entry: verify_models.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
| [verify_risk_models.py](../verify_risk_models.py) | Compatibility or validation entry: verify_risk_models.py. Inspect before running. (兼容或验收入口，执行前查看说明。) |
