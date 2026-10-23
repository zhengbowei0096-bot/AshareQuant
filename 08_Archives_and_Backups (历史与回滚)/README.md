# 08_Archives_and_Backups (历史与回滚)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Evidence and recoverable changes. Do not confuse backups with active code. (证据与可恢复变更，不作为当前运行代码。)

Recovery tools and instructions are included. Local backup archives and frozen account evidence are not published. (提供恢复工具与说明，本机备份及冻结账户证据不上传。)

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [asharequant/work_archive.py](../asharequant/work_archive.py) | Operations module: work_archive.py (按工作日期保存运行记录、源码快照和报告关联，供追溯和恢复。) |
| [scripts/organize_workspace.py](../scripts/organize_workspace.py) | Preview, refresh and restore workspace navigation. (预览、刷新和恢复分类导航。) |
| [docs/VALIDATION.md](../docs/VALIDATION.md) | Dated verification evidence and recovery instructions. (按日期记录的验证范围与恢复说明。) |
| [docs/SECURITY.md](../docs/SECURITY.md) | Publication boundaries and private data handling. (发布边界与私人数据保护。) |
