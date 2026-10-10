## MODIFIED Requirements

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

## ADDED Requirements

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