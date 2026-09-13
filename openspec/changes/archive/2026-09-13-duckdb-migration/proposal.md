# duckdb-migration 变更提案

## Why

stocksview 当前以 SQLite 为唯一数据库，而本项目的定位是**以 DuckDB 为本地分析内核的个人证券研究工具**：后续将承载千万级历史日线/分钟行情、复权因子与回测数据。SQLite 是行式、面向小型事务的嵌入式库，在批量扫描、时间序列聚合与列式分析上无法支撑这个方向；其 Alembic 迁移历史（batch 模式、PRAGMA 适配）也会持续拖累 schema 演进。

当前是切换持久层的最佳窗口：功能集稳定、架构分层清晰（API / Service / Repository / Provider / Jobs）、测试较完整，且历史行情数据尚未积累——趁地基好打的时候打，避免日后带数据重构。

本变更对应《stocksview DuckDB 演进版技术方案》（`stocksview_duckdb_technical_plan.md`）的第一个里程碑 **T01–T10**。

## What Changes

- **新建 marketmind 项目**：保留 stocksview 的分层架构与全部产品能力（A 股/港股股票、ETF、指数自选与标签、实时/延时行情、PE/PB/股息率、交易日历、行情缓存、后台刷新、Provider 切换与运行指标、健康检查、Docker 部署），以 `reference/stocksview` 为参考代码移植。
- **BREAKING** 数据库从 SQLite 整体切换为 DuckDB（`duckdb==1.5.5` + `duckdb-sqlalchemy==1.5.5.5`，锁定精确版本）：重写 `app/db.py`，清除全部 SQLite 专用逻辑（`PRAGMA foreign_keys`、`check_same_thread`、`.db` 路径处理），Alembic 去除 `render_as_batch`。
- **BREAKING** 全新 Alembic 基线 `0001_duckdb_baseline`，不继承 SQLite 迁移历史。核心表按技术方案 v1 结构重做（instrument、watchlist、index_watchlist、tag、watchlist_tag、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting）：以 `instrument_id`（如 `cn:stock:600519`）为业务主键，去掉无意义的自增代理 id；`quote_snapshot` 正式固定为"每只证券一行"的当前行情快照（ON CONFLICT upsert）；tag 显式使用 sequence。旧 SQLite 数据**不**通过 Alembic 迁移（独立导入工具属第二里程碑 T11–T16）。
- **BREAKING** 部署约束：DuckDB 是嵌入式单写者数据库，生产环境 Uvicorn 固定 1 个 worker，后台任务与 Web 请求同进程；应用内增加轻量写协调（短事务 + 进程内锁 + 有限重试）。
- Repository 层适配 DuckDB（重点：quote_snapshot upsert、复合主键、外键行为——DuckDB 不支持 `ON DELETE CASCADE`，删除顺序由 Service 显式保证）。
- API 与 UI 行为保持不变：现有端点响应结构与页面交互不动。
- 测试改造：Repository 集成测试与 migration 测试改跑真实临时 DuckDB 文件（不再用 SQLite 内存库代替）；新增 fresh-db migration 测试。
- 明确不做（第一版）：PostgreSQL / Redis / 微服务 / 消息队列、完整历史行情与回测实现、db_upgrade 自动备份与 SQLite 导入工具（T11–T16）、`/health` 的 revision 字段扩展（T15）——但表结构、命名规范与目录布局为这些扩展预留了位置。

## Capabilities

### New Capabilities

marketmind 是全新项目（`openspec/specs/` 为空），本变更首次建立全部能力规格。多数能力的行为规格自 stocksview 既有规格移植，作为"行为不变"的兼容性验收契约；受 DuckDB 影响的重写。

- `database-persistence`: DuckDB 引擎接入、连接与会话管理、单进程写入约束、写协调与冲突重试、quote_snapshot 单行语义（本变更的核心新增能力）
- `db-migration`: Alembic 版本账本——`0001_duckdb_baseline`、从零建库、人工审核迁移纪律、migration 失败阻止应用启动（自 stocksview 版针对 DuckDB 重写）
- `deployment`: Docker 单容器 + data volume 部署、强制单 worker、启动即自动迁移（按 DuckDB 调整）
- `watchlist-management`: 自选列表与指数自选管理（移植）
- `tag-management`: 标签管理与"被引用标签不可删除"保护（移植）
- `instrument-management`: 证券主数据与 `instrument_id` 标识管理（移植，主键语义变化）
- `quote-provider`: 行情 Provider（腾讯/akshare）与切换机制（移植）
- `fundamental-provider`: 估值 Provider（tushare）（移植）
- `quote-cache-refresh`: 行情缓存与后台刷新调度（移植，写路径适配 DuckDB）
- `market-session`: 交易时段判断与刷新门控（移植）
- `job-status`: 后台任务状态记录（移植）
- `provider-metrics`: Provider 运行指标（移植）
- `rest-api`: REST API 兼容契约——全部现有端点行为不变（移植）
- `dashboard-ui`: 页面 UI 兼容契约（移植）
- `config-management`: 配置管理（移植，`database.url` 指向 DuckDB 文件）
- `app-version`: 应用版本机制（移植）

### Modified Capabilities

（无——marketmind 当前没有既有规格）

## Impact

- **代码**：以 `reference/stocksview`（80 个 Python 文件）为参考移植为 marketmind 全新代码库；重点重写 `app/db.py`、`app/models/*`（10 张表）、`alembic/`（新基线）、`app/repositories/*`（upsert 路径）、`app/config.py`（数据库配置）、`Dockerfile` / `docker-compose.yml`（单 worker、`.duckdb` 卷）。
- **API/UI**：现有端点与页面行为保持不变；不新增、不删除、不修改任何现有端点的响应结构。
- **依赖**：新增 `duckdb==1.5.5`、`duckdb-sqlalchemy==1.5.5.5`（PyPI 最新版，与技术方案锁定版本一致）；SQLAlchemy / FastAPI / Alembic 等其余依赖沿用 stocksview 版本。
- **数据**：全新 DuckDB 数据库文件（`data/marketmind.duckdb`）；旧 stocksview SQLite 数据文件本里程碑不迁移、不受影响。
- **部署**：docker compose 单容器、单 worker、data volume；现有 SQLite 版 stocksview 部署**不能**原地升级到本版本（等第二里程碑的 `import_sqlite.py`）。
