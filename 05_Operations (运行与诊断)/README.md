# 05_Operations (运行与诊断)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Service configuration, health checks and logs. Services keep their existing paths and schedules. (服务配置、健康状态和日志，运行路径与时间不变。)

Scheduling and diagnostics code is included. Services must be installed locally; private service files and logs are excluded. (提供调度与诊断代码，服务须本机部署，私人服务文件和日志不上传。)

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [asharequant/cli.py](../asharequant/cli.py) | Operations module: cli.py (统一命令入口：分派 run、daily、forecast、decision、paper-account 等任务。) |
| [asharequant/daily.py](../asharequant/daily.py) | Operations module: daily.py (每日工作流水线：依次组织数据、分析、模拟账本、模型评估、验证和检查；记录步骤状态。) |
| [asharequant/scheduler.py](../asharequant/scheduler.py) | Operations module: scheduler.py (本地定时服务唤醒后的检查器：按北京时间和交易日判断是否应运行，记录尝试及结果；不能自行唤醒关机电脑。) |
| [asharequant/dynamic_service.py](../asharequant/dynamic_service.py) | Operations module: dynamic_service.py (行情驱动模拟、收盘结算、中文撤单原因；执行代码升级追加审核记录，不覆盖历史账本。) 报告保留最近盘中异常与经核验的恢复状态，休市不表示已恢复。 |
| [asharequant/mac_alerts.py](../asharequant/mac_alerts.py) | Operations module: mac_alerts.py (独立无声 Mac 弹窗：仅提醒服务异常及恢复，交易事件不弹窗；不调用模型、不修改账本。) |
| [asharequant/evaluation/connection_health.py](../asharequant/evaluation/connection_health.py) | Validation module: evaluation/connection_health.py (汇总数据连接及无人值守流程准备状态，保存检查证据。) |
| [docs/DYNAMIC_EXECUTION.md](../docs/DYNAMIC_EXECUTION.md) | Dynamic paper execution, prerequisites and limitations. (动态模拟执行、运行前提与限制。) |
