## MODIFIED Requirements

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