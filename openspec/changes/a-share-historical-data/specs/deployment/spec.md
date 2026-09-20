## MODIFIED Requirements

### Requirement: README

README SHALL 包含：项目介绍、环境要求、本地启动、Tushare 配置（config.yaml 方式）、Docker 启动、各资产类型字段支持情况、行情刷新说明（60 秒/午休/收盘/节假日停止/收盘补抓）、数据源与延迟说明、Provider 超时配置（providers.timeout）、以及升级思路说明。认证部署说明 SHALL 明确：全新部署完成迁移并启动服务后通过首次访问 `/setup` 创建第一个管理员；从旧版本升级的数据库可在受控网络中通过 `/setup` 认领迁移生成的占位管理员，或停服后使用现有 CLI 设置密码；CLI 与应用不得同时打开同一个 DuckDB 文件。v0.3.0 起 README 还 SHALL 包含历史数据功能说明：`/admin/data` 数据管理控制台的入口与用途、日级数据起点 2010-01-01 与四个日级数据集、首次回填可能持续较久且可跨多次启动完成、默认每日 20:30（Asia/Shanghai）定时同步与启动自动补齐、Tushare 2000 积分下的保守限流策略、以及 `history.*` 配置项说明。真实数据升级到 v0.3.0 前 SHALL 先备份 DuckDB 文件（如 `cp data/marketmind.duckdb data/marketmind.duckdb.bak`，Docker 部署按挂载路径处理）。

#### Scenario: 按文档部署

- **WHEN** 新用户按 README 操作并访问服务
- **THEN** 能完成配置、启动应用并通过 `/setup` 创建首个管理员后登录

#### Scenario: 升级数据库初始化

- **WHEN** v0.1.0 数据库升级到 v0.2.0 后启动服务
- **THEN** 既有数据保留，用户可在受控网络访问 `/setup` 认领占位管理员，完成后使用原有私有数据

#### Scenario: CLI 后备初始化

- **WHEN** 运维无法使用浏览器引导
- **THEN** 运维停止应用后可按 CLI 文档设置或创建管理员，重新启动应用后登录

#### Scenario: 升级前备份提示

- **WHEN** 使用者按 README 从 v0.2.0 升级到 v0.3.0
- **THEN** 文档明确要求先停止服务并备份 data/marketmind.duckdb，再执行迁移与启动

#### Scenario: 历史回填预期管理

- **WHEN** 使用者首次触发历史数据同步
- **THEN** README 说明回填从 2010-01-01 开始、可能受 Tushare 限流分多次运行完成、期间 /admin/data 持续展示进度
