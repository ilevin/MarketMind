# db-migration Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造,再以 `0003_a_share_historical_data` 引入 A 股历史数据表(migration 文件名 SHALL 基于实施时的实际 alembic head 创建,SHALL NOT 硬编码假定编号);SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级

- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id` sequence,以及历史数据表(cn_stock_basic、cn_stock_company、cn_stock_name_change、market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow、history_sync_state、history_day_status、history_sync_run、history_sync_run_dataset),alembic_version 记录最新版本

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

`0002_multi_user_auth` 迁移 SHALL 创建唯一的 legacy owner：用户名初始为 `admin`、角色为 `admin`、密码为不可登录的占位哈希、`must_change_password=true`，并将旧单用户数据全部归属该账户。后续首次访问 `/setup` 时，使用者提交的用户名和密码 SHALL 认领该账户；认领 SHALL 保留原 `user_id` 以及 watchlist、index_watchlist、tag、watchlist_tag 中的全部私有数据。迁移文件与配置 SHALL NOT 包含任何明文默认密码。

#### Scenario: 迁移占位账户可被自定义用户名认领
- **WHEN** v0.1.0 数据库升级到 v0.2.0，服务启动后在受控网络访问 `/setup` 并提交合法用户名和密码
- **THEN** 系统更新占位账户的用户名为提交值、写入新的 Argon2id 密码并关闭 `must_change_password`，原 `user_id` 与全部私有数据保持不变

### Requirement: 迁移数据校验

多用户迁移 SHALL 内置校验并在失败时整体回滚:新旧表行数一致(watchlist、index_watchlist、tag、watchlist_tag);所有新行 `user_id` 均存在于 app_user;所有 watchlist_tag 关联指向同一用户的 watchlist;标签关联关系完整保留。

#### Scenario: 行数校验
- **WHEN** 迁移拷贝数据后
- **THEN** 四张私有表的旧表行数 == 新表归属 legacy owner 的行数,不一致则迁移失败回滚

#### Scenario: 校验失败回滚
- **WHEN** 校验发现行数不一致或外键悬空
- **THEN** 迁移整体回滚,数据库保持 0001 版本结构,不残留半成品 v2 表

### Requirement: 升级部署流程

数据库升级 SHALL 由现有“容器启动先执行 `alembic upgrade head`，成功后才启动应用”机制自动完成，SHALL NOT 要求用户手动执行迁移。升级后，文档和启动日志 SHALL 优先提示在受控网络访问 `/setup` 认领迁移生成的占位管理员；首用户的用户名 SHALL 由使用者自行设置，不固定为 `admin`。无法使用浏览器时，运维停止应用后 SHALL 可使用现有 CLI 设置占位账户密码或创建管理员作为后备路径。CLI 与应用不得同时打开同一个 DuckDB 文件。

#### Scenario: 旧版本容器升级
- **WHEN** 使用含 0001 数据的旧数据库启动新版本容器
- **THEN** 启动过程自动完成 0002 迁移，应用就绪，旧数据归属占位 legacy owner，并提示通过 `/setup` 完成认领

#### Scenario: 迁移失败阻止启动
- **WHEN** 0002 迁移校验失败
- **THEN** 容器退出，uvicorn 不启动，数据库保持迁移前状态

#### Scenario: CLI 后备初始化
- **WHEN** 运维无法使用浏览器引导
- **THEN** 运维停止应用后可运行现有 CLI 设置占位账户密码或创建管理员，重新启动应用后登录；CLI 执行期间应用不得占用同一数据库文件

### Requirement: 0003 历史数据迁移约束

`0003_a_share_historical_data` 迁移 SHALL：扩展 `trading_calendar` 增加可空列 exchange、pretrade_date、source、fetched_at（简单加列直接 ALTER，现有 CalendarRepository 写路径无需修改即兼容）；创建 3 张主档表与 4 张同步控制表（ORM 模型）及 4 张日级事实表（SQLAlchemy Core Table，无物理主键/外键/索引）；SHALL NOT 修改或删除 `fundamental_snapshot`，SHALL NOT 将旧 fundamental 数据迁移进新表，SHALL NOT 破坏已有 instrument/watchlist/user 数据。从 0002 升级后既有表数据 SHALL 保持不变。

#### Scenario: 旧数据升级无损

- **WHEN** 含 v0.2.0 用户与自选数据的数据库执行 0003 迁移
- **THEN** app_user、watchlist、tag、fundamental_snapshot 等既有表行数与内容不变，新表为空可查询，trading_calendar 新列为 NULL

#### Scenario: 事实表建表形态

- **WHEN** 迁移后检查 market_daily_bar 等四张事实表
- **THEN** 列齐全且无主键/外键/二级索引；history_day_status 以 (dataset, trade_date) 为主键，history_sync_run_dataset 以 (run_id, dataset) 为主键

