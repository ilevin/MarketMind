# db-migration Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造;SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级
- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id` sequence,alembic_version 记录最新版本

#### Scenario: 迁移与模型一致
- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致,无 schema 漂移

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

### Requirement: 多用户数据迁移

`0002_multi_user_auth` 迁移 SHALL 完成:创建 `seq_user_id` sequence、`app_user`、`user_session` 表;以"建 v2 表 → 拷贝数据 → 校验 → 删旧表 → 改名"流程重建 `watchlist`、`index_watchlist`、`tag`、`watchlist_tag`(复合主键含 `user_id`);创建 legacy owner(username `admin`、role `admin`、不可登录的占位密码哈希、`must_change_password=true`);把旧单用户数据全部归属 legacy owner。迁移文件与配置 SHALL NOT 包含任何明文默认密码。

#### Scenario: 旧库升级归属 legacy owner
- **WHEN** 对含旧单用户数据(自选/指数/标签/关联)的数据库执行 `alembic upgrade head`
- **THEN** 全部旧数据完整归属 legacy owner(用户 admin),legacy owner 登录后可看到原有自选与标签

#### Scenario: 全局表不受迁移影响
- **WHEN** 多用户迁移执行前后统计 instrument/quote_snapshot/fundamental_snapshot/trading_calendar 行数
- **THEN** 行数不变

#### Scenario: 迁移后无明文密码
- **WHEN** 检查迁移产物中 app_user.password_hash
- **THEN** 仅为不可登录的占位哈希值,迁移与配置文件中不存在明文密码

### Requirement: 迁移数据校验

多用户迁移 SHALL 内置校验并在失败时整体回滚:新旧表行数一致(watchlist、index_watchlist、tag、watchlist_tag);所有新行 `user_id` 均存在于 app_user;所有 watchlist_tag 关联指向同一用户的 watchlist;标签关联关系完整保留。

#### Scenario: 行数校验
- **WHEN** 迁移拷贝数据后
- **THEN** 四张私有表的旧表行数 == 新表归属 legacy owner 的行数,不一致则迁移失败回滚

#### Scenario: 校验失败回滚
- **WHEN** 校验发现行数不一致或外键悬空
- **THEN** 迁移整体回滚,数据库保持 0001 版本结构,不残留半成品 v2 表

### Requirement: 升级部署流程

数据库升级 SHALL 由现有"容器启动先执行 `alembic upgrade head`,成功后才启动应用"机制自动完成,SHALL NOT 要求用户手动执行迁移;升级后 SHALL 提示(文档/日志)运行 `python -m app.cli users set-password admin` 完成 legacy owner 初始化。

#### Scenario: 旧版本容器升级
- **WHEN** 使用含 0001 数据的旧数据库启动新版本容器
- **THEN** 启动过程自动完成 0002 迁移,应用就绪,旧数据归属 legacy owner

#### Scenario: 迁移失败阻止启动
- **WHEN** 0002 迁移校验失败
- **THEN** 容器退出,uvicorn 不启动,数据库保持迁移前状态

