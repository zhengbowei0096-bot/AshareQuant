# 09_Environment (环境与依赖)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

requirements.txt is the basic installation list; config/environments/ keeps optional historical environment versions. (首页保留基础安装清单，历史环境版本集中保存。)

Dependency definitions are public; installed environments, third-party bundles and private certificates stay local. (依赖定义公开，已安装环境、第三方库副本和私人证书留在本机。)

## Environment_Snapshots (环境版本记录)

| File (文件) | Purpose (用途) |
|---|---|
| [config/environments/requirements-models.lock.txt](../config/environments/requirements-models.lock.txt) | Recorded model-research environment versions; optional historical reproduction. (预测研究环境版本记录。) |
| [config/environments/requirements-risk-models.lock.txt](../config/environments/requirements-risk-models.lock.txt) | Recorded risk-model environment versions; optional historical reproduction. (风险模型研究环境版本记录。) |
| [config/environments/requirements-stage1.lock.txt](../config/environments/requirements-stage1.lock.txt) | Recorded Stage 1 data-processing environment versions; optional historical reproduction. (数据阶段环境版本记录。) |

## Files (文件)

| File (文件) | Purpose (用途) |
|---|---|
| [requirements.txt](../requirements.txt) | Dependency versions for installation/reproduction. (安装与复现用依赖版本。) |

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [.gitignore](../.gitignore) | Exclude local accounts, keys, reports and generated artifacts from Git. (Git 排除规则；网页手动上传仍须检查文件。) |
