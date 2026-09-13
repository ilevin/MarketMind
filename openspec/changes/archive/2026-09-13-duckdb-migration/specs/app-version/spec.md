## ADDED Requirements

### Requirement: 版本号唯一来源

应用版本号 SHALL 统一定义于 `app/version.py`（`APP_VERSION = "v0.1.0"`），全部展示出口（页面、接口）SHALL 引用该常量，SHALL NOT 在模板或接口中硬编码版本字符串。

#### Scenario: 单一定义处
- **WHEN** 检索版本号来源
- **THEN** 仅 app/version.py 定义版本字符串，其他位置均为引用

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
