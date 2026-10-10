# app-version Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 版本号唯一来源

应用版本号 SHALL 统一定义于 `app/version.py`（`APP_VERSION`，形如 `"v0.4.2"`，带 `v` 前缀），全部运行时展示出口（页面页脚、`/health`、管理状态接口）SHALL 引用该常量，SHALL NOT 在模板或接口中硬编码版本字符串。该常量 SHALL 与 `pyproject.toml` 的 `version`（不带 `v` 前缀）保持一致，并由自动化一致性测试守护；Dockerfile 等构建产物 SHALL NOT 出现版本字面量。

#### Scenario: 单一定义处
- **WHEN** 检索版本号来源
- **THEN** 运行时展示出口仅引用 `app/version.py` 的 `APP_VERSION`，模板与接口中不存在硬编码的版本字符串

#### Scenario: 与打包元数据一致
- **WHEN** 读取 `app/version.py` 的 `APP_VERSION` 与 `pyproject.toml` 的 `version`
- **THEN** `APP_VERSION` 等于 `"v"` 与 `version` 的拼接（`v0.4.2` 对应 `0.4.2`）

#### Scenario: 版本漂移被测试拦截
- **WHEN** 仅更新 `pyproject.toml` 与 `app/version.py` 中的一处版本号并运行离线测试
- **THEN** 一致性测试失败并指明两处版本不一致，发布前即可发现漂移，而不再依赖人工在提交时同步多处

### Requirement: 行情页脚版本信息

行情页面底部 SHALL 显示 `marketmind v0.1.0`（品牌取新项目名，版本取自 APP_VERSION，经模板注入），作为页面最底部独立区块。

#### Scenario: 页脚显示版本
- **WHEN** 访问行情首页 /
- **THEN** 页面最底部显示 marketmind v0.1.0

### Requirement: 健康接口返回版本

`GET /health` 响应 SHALL 增加 `version` 字段（取自 APP_VERSION），用于确认线上运行版本，其余字段（status、database）保持不变。

#### Scenario: 健康检查带版本
- **WHEN** GET /health 且数据库可连接
- **THEN** 返回 `{"status":"ok","database":"ok","version":"v0.1.0"}`

