## MODIFIED Requirements

### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造,再以 `0003_a_share_historical_data` 引入 A 股历史数据表(migration 文件名 SHALL 基于实施时的实际 alembic head 创建,SHALL NOT 硬编码假定编号);SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级

- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id` sequence,以及历史数据表(cn_stock_basic、cn_stock_company、cn_stock_name_change、market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow、history_sync_state、history_day_status、history_sync_run、history_sync_run_dataset),alembic_version 记录最新版本

#### Scenario: 迁移与模型一致

- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致,无 schema 漂移

## ADDED Requirements

### Requirement: 0003 历史数据迁移约束

`0003_a_share_historical_data` 迁移 SHALL：扩展 `trading_calendar` 增加可空列 exchange、pretrade_date、source、fetched_at（简单加列直接 ALTER，现有 CalendarRepository 写路径无需修改即兼容）；创建 3 张主档表与 4 张同步控制表（ORM 模型）及 4 张日级事实表（SQLAlchemy Core Table，无物理主键/外键/索引）；SHALL NOT 修改或删除 `fundamental_snapshot`，SHALL NOT 将旧 fundamental 数据迁移进新表，SHALL NOT 破坏已有 instrument/watchlist/user 数据。从 0002 升级后既有表数据 SHALL 保持不变。

#### Scenario: 旧数据升级无损

- **WHEN** 含 v0.2.0 用户与自选数据的数据库执行 0003 迁移
- **THEN** app_user、watchlist、tag、fundamental_snapshot 等既有表行数与内容不变，新表为空可查询，trading_calendar 新列为 NULL

#### Scenario: 事实表建表形态

- **WHEN** 迁移后检查 market_daily_bar 等四张事实表
- **THEN** 列齐全且无主键/外键/二级索引；history_day_status 以 (dataset, trade_date) 为主键，history_sync_run_dataset 以 (run_id, dataset) 为主键
