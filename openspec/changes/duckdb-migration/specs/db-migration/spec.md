## ADDED Requirements

### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构：提供 `alembic.ini` 与 `alembic/versions/` 迁移目录；版本链以全新基线 `0001_duckdb_baseline` 开始，一次性创建全部 10 张核心表与 `seq_tag_id` sequence；SHALL NOT 继承 stocksview 的 SQLite 迁移历史（0001_v002_baseline、0002_v003、0003_v003b 均废弃，不移植、不 stamp）。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级
- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 一次性建立全部 10 张核心表与 `seq_tag_id` sequence，alembic_version 记录版本 `0001_duckdb_baseline`

#### Scenario: 迁移与模型一致
- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致，无 schema 漂移

### Requirement: 应用启动时自动迁移

容器启动流程 SHALL 先执行 `alembic upgrade head`，成功后才启动应用；迁移失败时应用 SHALL NOT 启动（容器退出），避免代码与数据库结构版本不一致。应用 lifespan SHALL NOT 再以 `create_all` 建表；`create_all` 仅保留给测试环境使用。

#### Scenario: 全新容器首次启动
- **WHEN** 无历史数据时启动容器
- **THEN** 启动过程自动完成建表迁移，随后应用就绪

#### Scenario: 迁移失败阻止启动
- **WHEN** `alembic upgrade head` 执行失败
- **THEN** 容器退出，uvicorn 不启动，数据库保持迁移前状态

### Requirement: DuckDB 结构变更策略

基线之后的简单结构变更（新增可空列、重命名列、添加默认值等）SHALL 直接使用 `ALTER TABLE`；复杂变更（修改主键、增删复杂约束、危险类型转换）SHALL 采用"创建新表 → INSERT SELECT 搬数据 → 行数与业务键校验 → 删除旧表 → 新表改名"流程。alembic autogenerate 产物 SHALL 经人工审核后才可提交；涉及数据搬迁的 migration SHALL 内置升级前后行数与业务键完整性校验，校验失败 SHALL 回滚。

#### Scenario: 简单加列直接 ALTER
- **WHEN** 后续迁移为已有表新增一个可空列
- **THEN** 以单条 `ALTER TABLE ... ADD COLUMN` 完成，不进行表重建

#### Scenario: 复杂变更数据校验失败回滚
- **WHEN** 复杂变更（如修改主键）执行"建新表搬数据"流程，且搬数据后行数校验失败
- **THEN** 迁移整体回滚，数据库保持变更前状态，不残留半成品新表
