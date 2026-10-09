## MODIFIED Requirements

### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造,再以 `0003_a_share_historical_data` 引入 A 股历史数据表,再以 `0004` 变更引入个股水位表与任务流水表,再以 `0005` 变更引入 ETF 数据表(cn_etf_basic、etf_daily、etf_adj_factor)(migration 文件名 SHALL 基于实施时的实际 alembic head 创建,SHALL NOT 硬编码假定编号);SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级

- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id`、`seq_sync_task_id` sequence,以及历史数据表(cn_stock_basic、cn_stock_company、cn_stock_name_change、market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow、history_sync_state、history_day_status、history_sync_run、history_sync_run_dataset、stock_sync_state、sync_task),以及 ETF 数据表(cn_etf_basic、etf_daily、etf_adj_factor),alembic_version 记录最新版本

#### Scenario: 迁移与模型一致

- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致,无 schema 漂移

## ADDED Requirements

### Requirement: 0005 ETF 数据模块迁移约束

`0005` 迁移（migration 文件名 SHALL 基于实施时的实际 alembic head 创建，SHALL NOT 硬编码假定编号）SHALL 创建三张新表：`cn_etf_basic`（instrument_id 主键 + 外键约束→instrument.instrument_id，命名沿 0003 `cn_stock_basic` 先例、DuckDB 落库为方言规范化名）、`etf_daily` 与 `etf_adj_factor`（SQLAlchemy Core Table 风格：无物理主键/外键/二级索引）。SHALL NOT 修改或删除任何既有表、列、sequence 与数据（含 `stock_sync_state`——ETF 数据集按 (dataset, instrument_id) 键空间补建状态行，属运行时行为而非迁移行为）；SHALL NOT 写入任何业务数据行（新表为空可查询，ETF universe 与状态行由首轮同步建立）。迁移 SHALL 可重复执行（对已迁移库幂等跳过）、SHALL 内置既有表行数不变的纯增量校验并在校验失败时整体回滚。downgrade SHALL 仅提供 DDL 逆操作（删除三张新表），生产回滚以文件备份为准（同 0002/0003/0004 约定）。

#### Scenario: v0.4.1 数据升级无损

- **WHEN** 含 v0.4.1 全部历史数据与同步状态的数据库执行 0005 迁移
- **THEN** 既有全部表行数与内容不变，cn_etf_basic/etf_daily/etf_adj_factor 为空可查询，alembic_version 为最新版本

#### Scenario: 全新库全链建表

- **WHEN** 空数据库执行 alembic upgrade head
- **THEN** 0001→0005 全链执行，三张 ETF 新表随链建立

#### Scenario: 幂等重放

- **WHEN** 对已完成 0005 迁移的数据库再次执行迁移
- **THEN** 迁移幂等跳过（无重复建表、无错误），数据库内容不变

#### Scenario: 迁移失败回滚

- **WHEN** 0005 执行中校验失败
- **THEN** 迁移整体回滚，数据库保持 0004 版本结构，无半成品表残留
