## MODIFIED Requirements

### Requirement: 本地启动

项目 SHALL 支持 Python 3.11+ 本地启动：venv + `pip install -e .` + 复制配置 + `alembic upgrade head` + `uvicorn app.main:app`。服务启动后，全新部署或仅含迁移占位管理员的数据库 SHALL 可通过浏览器访问 `/setup` 完成第一个 admin 用户初始化；已存在真实用户的数据库 SHALL 直接进入登录流程。初始化前 SHALL 不再要求额外执行设置管理员密码 CLI 命令。

#### Scenario: 本地运行
- **WHEN** 按 README 步骤本地启动
- **THEN** 服务运行且 GET /health 返回 200

#### Scenario: 本地首次访问引导
- **WHEN** 本地服务启动后数据库未初始化且用户访问 `/`
- **THEN** 用户被引导至 `/setup`，完成合法首用户表单后可登录并访问首页

#### Scenario: 已有用户直接登录
- **WHEN** 本地服务启动后数据库已存在真实用户且用户访问 `/`
- **THEN** 用户被引导至 `/login`，系统不显示首用户创建流程

### Requirement: README

README SHALL 包含：项目介绍、环境要求、本地启动、Tushare 配置（config.yaml 方式）、Docker 启动、各资产类型字段支持情况、行情刷新说明（60 秒/午休/收盘/节假日停止/收盘补抓）、数据源与延迟说明、Provider 超时配置（providers.timeout）、以及升级思路说明。认证部署说明 SHALL 明确：全新部署完成迁移并启动服务后通过首次访问 `/setup` 创建第一个管理员；从旧版本升级的数据库可在受控网络中通过 `/setup` 认领迁移生成的占位管理员，或停服后使用现有 CLI 设置密码；CLI 与应用不得同时打开同一个 DuckDB 文件。

#### Scenario: 按文档部署
- **WHEN** 新用户按 README 操作并访问服务
- **THEN** 能完成配置、启动应用并通过 `/setup` 创建首个管理员后登录

#### Scenario: 升级数据库初始化
- **WHEN** v0.1.0 数据库升级到 v0.2.0 后启动服务
- **THEN** 既有数据保留，用户可在受控网络访问 `/setup` 认领占位管理员，完成后使用原有私有数据

#### Scenario: CLI 后备初始化
- **WHEN** 运维无法使用浏览器引导
- **THEN** 运维停止应用后可按 CLI 文档设置或创建管理员，重新启动应用后登录
