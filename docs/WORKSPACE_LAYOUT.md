# Workspace Layout (文件分类)

The desktop project opens as ten numbered, English-first categories. Chinese notes explain each purpose. Files are accessed through relative links; the original runtime paths stay in place because services, frozen models and code checks depend on them. This is a presentation reorganization, not a relocation of the trading engine. (桌面按编号分类，通过相对链接访问；后台原路径保留。)

| Folder | Purpose |
|---|---|
| `00_Start_Here (使用入口)` | Start with the guide, daily overview and live account. (先看指南、每日总览和实时账户。) |
| `01_Reports (报告与收益)` | Read the main account, predictions and validation. Archived snapshots keep their original dates. (查看主账户、预测、验证及按日期保存的历史报告。) |
| `02_Source (程序源码)` | Read modules in execution order; links keep runtime paths and source fingerprints intact. (按数据到运行顺序查看源码，原路径与指纹保留。) |
| `03_Configuration (配置)` | Local settings and public examples; do not change risk limits by moving files. (本机配置与公开模板，整理不改变参数。) |
| `04_Data (数据与账本)` | Private runtime data. Keep ledgers and frozen research records intact. (私人运行数据，保留账本和冻结研究证据。) |
| `05_Operations (运行与诊断)` | Service configuration, health checks and logs. Services keep their existing paths and schedules. (服务配置、健康状态和日志，运行路径与时间不变。) |
| `06_Tests_and_Tools (测试与工具)` | Automated tests and explicit maintenance tools; verification scripts can have side effects. (自动测试与维护工具，验收脚本可能联网或写记录。) |
| `07_Documentation (说明文档)` | Current manuals separated from historical implementation notes. (当前使用说明与历史开发记录分开。) |
| `08_Archives_and_Backups (历史与回滚)` | Evidence and recoverable changes. Do not confuse backups with active code. (证据与可恢复变更，不作为当前运行代码。) |
| `09_Environment (环境与依赖)` | Dependency definitions, vendor library and required certificate chain. (依赖定义、第三方库及必要证书链。) |

## Everyday use (日常使用)

Open `00_Start_Here (使用入口)` and double-click `Open_Daily_Overview (每日总览).command` or `Open_Live_Account (实时账户).command`. These only open reports; they do not submit orders or start another analysis run. Use `File_Guide.html` to find source responsibilities. Each category includes `00_README.html` and `00_README.md`.

The live account remains a paper ledger. Historical source reports retain their original filenames, dates and values. (实时账户仍为模拟记账；历史文件名称、日期和原始数据不改写。)

## Refresh classification (维护分类)

```bash
python scripts/organize_workspace.py --root .
python scripts/organize_workspace.py --root . --apply
```

The first command previews coverage. Unknown root entries or unclassified Python modules stop the refresh until `config/workspace_layout.json` and file descriptions are updated. The second refreshes generated categories and guides. It refuses to overwrite user-added files, edited links or edited generated guides. Normal growing report/data directories remain in their canonical location; add new functional groups to the layout when functionality changes.

On macOS, original root entries are hidden in Finder so that the categories remain prominent. This does not rename them, change file contents or change Python imports. `Command+Shift+.` reveals Finder-hidden items when investigating the original runtime layout. Other platforms generate the categories without applying Finder flags.

`--clean-cache` removes only Python bytecode (`.pyc`/`.pyo`) and Finder metadata in the active source/test/tool/vendor trees. A verified ZIP is written before removal. Caches can be regenerated during normal execution, so they may reappear. Logs, credentials, certificates, dependency code, frozen reports, account databases and research evidence are retained. (只清理可再生成缓存，不删日志、账户或研究证据。)

## Recovery (恢复)

Each refresh saves the previous folder view, Finder flags and affected guide documents under `backups/workspace-organization/`. The command prints the exact backup path. Run:

```bash
python scripts/organize_workspace.py --root . --rollback "backups/workspace-organization/<backup-id>"
```

Rollback restores the previous view and deleted caches. It does not reset a trading account, rewrite historical fills, change model parameters or alter service schedules. On an unmanaged-file conflict, preserve that file before refreshing; do not bypass the check by deleting it.

## Validation scope (验证范围)

Validated on 2026-10-23: reversible organization, complete Python source classification, refusal of unknown or manually edited files, original input preservation and cleanup restore. Runtime checks also cover imports, CLI entry, source fingerprints, launch-agent paths and report links. No new strategy, prediction or model-validation claim follows from this filesystem work.

## GitHub synchronization (GitHub同步)

The public repository is GuanlanQuant; the runtime package and established local paths remain AshareQuant / `asharequant`. The repository contains the same category definition, maintenance script and file guides as the desktop workspace. Markdown source links open directly on GitHub; HTML guides are intended for local browsers. GitHub sorts its own file list, so the numbered view is reproduced locally by the organizer, not by moving fingerprint-bound source files.

Private categories are documented by purpose only. Account databases, holdings, credentials, logs, reports, snapshots, backups, installed service files and generated navigation links are excluded from publication. A fresh clone has no account or report data; missing local-only targets are skipped when building links. Optional `.venv` and `venv` folders belong to Environment and stay private.

Initialization uses `config/dynamic_config.example.json` to create `dynamic_config.json`; it never overwrites an existing configuration and leaves the dynamic service disabled by default. Refresh the layout after initialization. (配置模板、文件说明和导航一起维护；公开仓库不包含个人运行数据。)
