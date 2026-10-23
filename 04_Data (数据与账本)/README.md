# 04_Data (数据与账本)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Private runtime data. Keep ledgers and frozen research records intact. (私人运行数据，保留账本和冻结研究证据。)

Data retrieval, storage and paper ledger code is included. Accounts, holdings, market caches and transaction records are private. (保留取数、存储及模拟账本功能，账户、持仓、行情缓存与成交记录不上传。)

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [asharequant/data.py](../asharequant/data.py) | Data module: data.py (日历、历史行情增量更新、批量更新及报价获取。) |
| [asharequant/data_sources/database.py](../asharequant/data_sources/database.py) | Data module: data_sources/database.py (把校验后的行情快照同步到 SQLite 数据库，避免重复导入。) |
| [asharequant/data_sources/broker_records.py](../asharequant/data_sources/broker_records.py) | Data module: data_sources/broker_records.py (读取本地券商 CSV 导出记录；导入文件不等于实时连接券商。) |
| [asharequant/history_sources.py](../asharequant/history_sources.py) | Data module: history_sources.py (历史数据及执行状态证据获取，保留来源；未知公司行动不当作没有发生。) |
| [asharequant/intraday_data.py](../asharequant/intraday_data.py) | Data module: intraday_data.py (盘中模拟所需的报价、交易日和公司行动参考资料及质量检查。) |
| [asharequant/order_engine.py](../asharequant/order_engine.py) | Execution and Ledger module: order_engine.py (动态模拟委托生命周期、后续报价撮合、T+1、资金预留、部分成交费用及阶段盈亏；无真实券商通道。) |
| [docs/SECURITY.md](../docs/SECURITY.md) | Publication boundaries and private data handling. (发布边界与私人数据保护。) |
