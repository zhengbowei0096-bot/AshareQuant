# 09_Environment (环境与依赖)

[Project home (项目首页)](../README.md) · [File guide (文件说明)](../docs/FILE_GUIDE.md)

Dependency definitions, vendor library and required certificate chain. (依赖定义、第三方库及必要证书链。)

Dependency definitions are public; installed environments, third-party bundles and private certificates stay local. (依赖定义公开，已安装环境、第三方库副本和私人证书留在本机。)

## Files (文件)

| File (文件) | Purpose (用途) |
|---|---|
| [requirements-models.lock.txt](../requirements-models.lock.txt) | Dependency versions for installation/reproduction. (安装与复现用依赖版本。) |
| [requirements-risk-models.lock.txt](../requirements-risk-models.lock.txt) | Dependency versions for installation/reproduction. (安装与复现用依赖版本。) |
| [requirements-stage1.lock.txt](../requirements-stage1.lock.txt) | Dependency versions for installation/reproduction. (安装与复现用依赖版本。) |
| [requirements.txt](../requirements.txt) | Dependency versions for installation/reproduction. (安装与复现用依赖版本。) |

## Implementation and Guides (功能与使用说明)

| File (文件) | Purpose (用途) |
|---|---|
| [.gitignore](../.gitignore) | Exclude local accounts, keys, reports and generated artifacts from Git. (Git 排除规则；网页手动上传仍须检查文件。) |
