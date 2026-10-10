# deployment Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: Dockerfile

项目 SHALL 提供 Dockerfile，镜像内 SHALL 以离线模式安装应用：复制预构建的 wheels 目录后以 `pip install --no-index --find-links` 安装，SHALL NOT 在构建期访问 PyPI。安装的应用版本 SHALL 由构建参数 `APP_VERSION` 提供（形如 `0.4.2`，不带 `v` 前缀），Dockerfile SHALL NOT 出现版本字面量；构建期未提供该参数时 SHALL 在安装步骤立即以明确错误信息失败，SHALL NOT 退化为安装任意版本。镜像 SHALL 包含 Alembic 迁移资产（alembic.ini 与 alembic/ 目录）及 alembic 依赖；容器启动命令 SHALL 先执行 `alembic upgrade head`，成功后再以 uvicorn `--workers 1`（显式单 worker）启动应用，迁移失败 SHALL 使容器退出。

#### Scenario: 构建镜像

- **WHEN** 在已生成 wheels 的前提下以 `--build-arg APP_VERSION=<当前版本>` 执行 `docker build -t marketmind .`
- **THEN** 成功产出可运行镜像，镜像内含 Alembic 迁移资产，且安装的应用版本与 `pyproject.toml` 一致

#### Scenario: 缺少版本参数时快速失败

- **WHEN** 执行 `docker build` 而未提供 `APP_VERSION` 构建参数
- **THEN** 构建在安装步骤立即失败，错误信息指明需传入 `APP_VERSION`，不发生「构建成功但装入了非预期版本」

#### Scenario: 版本参数与 wheels 不匹配

- **WHEN** 传入的 `APP_VERSION` 与 wheels 目录内实际 wheel 的版本不一致
- **THEN** 离线解析找不到匹配发行版，构建失败并报出该版本号，不静默安装其他版本

#### Scenario: 容器启动自动迁移

- **WHEN** 启动容器且数据库为空或落后于最新迁移
- **THEN** 启动流程先完成 alembic upgrade head，随后应用就绪

#### Scenario: 迁移失败容器退出

- **WHEN** 容器启动时 alembic upgrade head 失败
- **THEN** 容器以非零码退出，uvicorn 不启动

### Requirement: 镜像构建脚本

项目 SHALL 提供 `scripts/build-docker.sh` 作为镜像构建入口：脚本 SHALL 为当前代码构建 wheel 及全部运行时依赖 wheel，SHALL 从 `pyproject.toml` 读取版本（构建链路中的唯一版本解析点）并经 `--build-arg APP_VERSION` 注入镜像构建。当镜像标签形如版本号（`v` 后接数字）且与 `pyproject.toml` 版本不一致时，脚本 SHALL 在构建镜像前中止并提示不一致，SHALL NOT 产出标签声称版本与镜像内容不符的镜像。

#### Scenario: 按当前版本构建

- **WHEN** 执行 `./scripts/build-docker.sh v<当前 pyproject 版本>`
- **THEN** 脚本生成 wheels、注入该版本并成功产出 `marketmind:v<当前版本>` 镜像

#### Scenario: 标签与项目版本不一致时中止

- **WHEN** 执行 `./scripts/build-docker.sh v1.2.3` 而 `pyproject.toml` 版本为 `0.4.2`
- **THEN** 脚本在构建镜像前中止并提示标签与项目版本不一致，不产出镜像

#### Scenario: 非版本标签不受限制

- **WHEN** 执行 `./scripts/build-docker.sh latest` 或自定义的非版本标签
- **THEN** 脚本不因一致性校验中止，仍按 `pyproject.toml` 当前版本注入并构建镜像

### Requirement: docker-compose 单容器运行

项目 SHALL 提供 docker-compose.yml，仅运行一个应用容器（无数据库容器），挂载 `./data:/app/data` 与 `./config.yaml:/app/config.yaml:ro`；执行 `cp config.example.yaml config.yaml && docker compose up -d` 后 SHALL 可通过 http://localhost:8000 访问。

#### Scenario: compose 启动
- **WHEN** 复制并填写配置后执行 docker compose up -d
- **THEN** 单容器启动，首页可访问，DuckDB 数据库文件 data/marketmind.duckdb（及 .wal）持久化在宿主机 ./data 卷

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

