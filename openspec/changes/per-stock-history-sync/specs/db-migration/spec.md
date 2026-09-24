## MODIFIED Requirements

### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造,再以 `0003_a_share_historical_data` 引入 A 股历史数据表,再以 `0004` 变更引入个股水位表与任务流水表(migration 文件名 SHALL 基于实施时的实际 alembic head 创建,SHALL NOT 硬编码假定编号);SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级

- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id`、`seq_sync_task_id` sequence,以及历史数据表(cn_stock_basic、cn_stock_company、cn_stock_name_change、market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow、history_sync_state、history_day_status、history_sync_run、history_sync_run_dataset、stock_sync_state、sync_task),alembic_version 记录最新版本

#### Scenario: 迁移与模型一致

- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致,无 schema 漂移

## ADDED Requirements

### Requirement: 0004 个股水位迁移约束

`0004` 迁移 SHALL：创建 `stock_sync_state`（(dataset, instrument_id) 逻辑唯一键，无 UNIQUE 约束/外键/二级索引）与 `sync_task`（id 为 BIGINT，由显式 sequence `seq_sync_task_id` 生成，无 UNIQUE 约束/外键/二级索引）两张表；为 `history_sync_run_dataset` 增加统计列 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count`（server_default 0，简单 `op.add_column`）。SHALL NOT 写入任何 `stock_sync_state` 行——初始个股水位统一为 NULL（首轮运行时按 universe 批量补建状态行），SHALL NOT 尝试从事实数据或旧数据集水位推导个股水位（旧整日水位无法证明个股无缺口；宁可重复拉取、不可错误跳过缺口）。SHALL 附带只读诊断统计写入迁移日志（各数据集事实行数、有数据股票数、最大交易日分布）帮助运维预估回填规模，诊断 SHALL NOT 修改任何业务数据或推进任何水位。SHALL NOT 修改或删除任何既有表、列与数据；旧 `history_sync_state.latest_complete_trade_date` 值原样冻结保留。迁移 SHALL 可重复执行（对已迁移库幂等）并在结构校验失败时整体回滚。downgrade SHALL 仅提供 DDL 逆操作（删除新增表/列/sequence），生产回滚以文件备份为准。

#### Scenario: v0.3.1 数据升级无损

- **WHEN** 含 v0.3.1 历史数据与同步状态的数据库执行 0004 迁移
- **THEN** 既有全部表行数与内容不变，stock_sync_state 与 sync_task 为空可查询，run_dataset 新列全部为 0，旧水位字段保持迁移前值

#### Scenario: 全新库全链建表

- **WHEN** 空数据库执行 alembic upgrade head
- **THEN** 0001→0004 全链执行，新表与 sequence 随链建立，alembic_version 为最新版本

#### Scenario: 诊断输出帮助预估

- **WHEN** 含大量事实数据的库执行 0004 迁移
- **THEN** 迁移日志输出各数据集行数、有数据股票数与最大交易日统计，且不修改任何业务数据

#### Scenario: 迁移失败回滚

- **WHEN** 0004 执行中结构校验失败
- **THEN** 迁移整体回滚，数据库保持 0003 版本结构，无半成品表残留
