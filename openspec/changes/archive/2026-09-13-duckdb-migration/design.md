# duckdb-migration 技术设计

> 事实来源：`stocksview_duckdb_technical_plan.md`（技术方案全文）+ `research/stocksview-*.md`（6 份代码库分析报告，含 file:line 级现状事实）。
> 本设计覆盖第一个里程碑 T01–T10；T11–T16（升级工具链）与历史行情/回测不在本变更内。

## Context

**现状**：stocksview（v0.03.1，参考克隆于 `reference/stocksview`）是 FastAPI + Jinja2 + 原生 JS 的个人行情看板：A 股/港股股票、ETF、指数自选与标签、实时/延时行情、PE/PB/股息率、交易日历缓存、60 秒后台刷新、Provider 指标、Alembic 迁移（0001_v002_baseline → 0003_v003b）、Docker 单容器部署。全部业务数据在单个 SQLite 文件 `data/market.db`。

**关键现状事实**（详见 research 报告）：

- 仓储/服务层 **0 处** SQLite 专用 SQL（无 `INSERT OR REPLACE`/`ON CONFLICT`/方言分支）；所有 upsert 是应用层 SELECT-then-INSERT/UPDATE。SQLite 耦合集中在 `app/db.py`（PRAGMA foreign_keys、check_same_thread、目录处理）、`alembic/env.py`（render_as_batch）、配置默认 URL、以及时间列的 naive/aware 混用。
- 8 张表使用 SQLite 自增代理 `id`；`quote_snapshot` 无唯一约束，"每证券一行"靠 Repository 原地 UPDATE 模式维持；`watchlist_tag` 的 `watchlist_id` 外键带 `ON DELETE CASCADE`（级联清理依赖 PRAGMA）。
- 并发模型：全局唯一 Engine + sessionmaker(autoflush=False, expire_on_commit=False)，请求与后台 Job 各自短 Session，**全项目无锁**——正确性依赖 SQLite 文件级单写者。
- 时间语义混乱：`job_status` 刻意写 naive 北京时间（补偿 SQLite 丢时区），`quote_snapshot.fetched_at` 写 aware 读回 naive，`/api/admin/status` 的 `_iso_beijing` 对两种形态分别处理。
- 测试：18 个文件，无共享 conftest；unit 用 SQLite 内存库 + `init_db`（create_all），integration 用临时文件库 + TestClient 注入假件，`test_migrations.py` 走 Alembic programmatic API。

**目标**：新项目 marketmind 以 DuckDB 为唯一数据库，功能与 API/UI 行为不变，表结构为历史行情与回测预留正确地基（业务主键、当前/历史数据分离、单进程写入）。

**约束**：DuckDB 是嵌入式单写者分析型数据库；`duckdb-sqlalchemy` 是第三方方言（非 DuckDB 官方维护）；DuckDB 不支持外键 `ON DELETE CASCADE`，不支持通用 `ADD/DROP CONSTRAINT`；并发写采用乐观冲突检测。

## Goals / Non-Goals

**Goals:**

- stocksview 全部功能在 DuckDB 上完整运行，现有 18 个 API 端点、3 个页面、错误码与响应 JSON 结构逐项保持不变（兼容契约见各 spec）。
- 10 张 v1 核心表按业务主键重建：`instrument_id` 直接作主键、复合主键取代代理 id、`quote_snapshot` 一证券一行（PK + upsert）、显式 sequence、统一 TIMESTAMPTZ。
- 全新 Alembic 基线 `0001_duckdb_baseline`，从空库 `alembic upgrade head` 可完整建库。
- 单进程写入模型落实：uvicorn 显式 `--workers 1`，应用内写协调（序列化写事务 + 有限重试）。
- 测试体系跑真实临时 DuckDB 文件（Repository 集成 + migration 从零/防漂移），不再用 SQLite 内存库冒充。
- 为第二里程碑（db_upgrade/备份/导入）与历史行情（`market_daily_bar`、`data_sync_state`）预留目录与命名规范，不实现。

**Non-Goals:**

- 不迁移旧 SQLite 数据（`import_sqlite.py` 属 T14）。
- 不实现 `scripts/db_upgrade.py`、升级前 CHECKPOINT、自动备份（T11–T12）。
- 不实现 `/health` 的 `database_revision` 字段（T15，`/health` 响应结构保持现状）。
- 不建历史行情/回测表，不做 Parquet、多 schema、Redis、微服务、多 worker。
- 不重构 stocksview 的分层架构（API / Service / Repository / Provider / Jobs 原样保留）；不加认证。

## Decisions

### D1. DuckDB 为唯一数据库，依赖锁精确版本

`duckdb==1.5.5` + `duckdb-sqlalchemy==1.5.5.5`（2026-09-13 PyPI 最新版，与技术方案锁定一致），在 pyproject 中写死精确版本，不用 `>=`。SQLAlchemy 2.x、Alembic、FastAPI 等其余依赖沿用 stocksview 的约束。

- **备选：PostgreSQL**——拒绝（目标是以 DuckDB 为本地分析内核的嵌入式个人工具，引入 server 容器违背产品形态）；**SQLite + 独立分析库**——拒绝（两套数据库的事务/备份/版本管理复杂度远超收益）。
- 第三方方言风险通过三点控制：锁版本、DuckDB 特有代码只出现在 `app/db.py` 与 Repository 的 upsert 语句中、升级依赖前必须跑 migration + 集成测试。

### D2. 新项目、新基线，不继承 SQLite Alembic 历史

新项目 Alembic 只有一条 `0001_duckdb_baseline`（revision id 字面量），一次性创建全部 10 张表 + `seq_tag_id` sequence。旧迁移链（0001_v002_baseline → 0003_v003b）不移植、不 stamp——SQLite 与 DuckDB 是不同数据库，历史链对新装用户是死代码。旧数据迁移走未来的独立导入工具（T14）。

- `alembic/env.py` 保留"URL 与运行时同源"设计（`_database_url()` 优先 programmatic 注入、回退 `load_config().database.url`），只删 `render_as_batch=True`（离线/在线两处）。
- 迁移纪律：autogenerate 仅作草稿，所有迁移人工审核；简单变更直接 `ALTER`，复杂变更（改主键/约束/危险类型）统一"建新表 → INSERT SELECT 搬数据 → 校验行数 → 换名"，搬数据的 migration 必须内置行数与业务键校验。
- downgrade：baseline 的 downgrade 为全量 DROP（可回空库）；不承诺任意 downgrade，破坏性变更的正式回滚方式是恢复备份（第二里程碑提供工具）。

### D3. v1 表结构：业务主键，去代理 id，无级联

以技术方案 §9–§14 的 DDL 为准，结合现状研究的差异清单，逐表定案：

| 表 | 主键 | 关键变化（相对现状） |
|---|---|---|
| `instrument` | `instrument_id VARCHAR` | 去掉自增 id；`currency`/`exchange` 均可空（方案 §9.1；现状 currency NOT NULL，调用方始终传值，放开无行为影响） |
| `watchlist` | `instrument_id` | 去掉 id 与 `uq_watchlist_instrument`（PK 即唯一）；外键补名 `fk_watchlist_instrument`；`sort_order` 加 server_default 0 |
| `index_watchlist` | `instrument_id` | 同上 |
| `tag` | `tag_id BIGINT DEFAULT nextval('seq_tag_id')` | 显式 `CREATE SEQUENCE seq_tag_id START 1`，不依赖 SQLite INTEGER PRIMARY KEY 自增 |
| `watchlist_tag` | `(instrument_id, tag_id)` 复合主键 | 去掉 id；关联键从 `watchlist_id`（整数）改为 `instrument_id`；外键**均不带 ondelete**（DuckDB 无级联） |
| `quote_snapshot` | `instrument_id` | 去掉 id；PK 直接保证一证券一行；补外键 → instrument；`change_percent`/`volume_ratio` 精度从 (10,4) 提到 DECIMAL(12,6)，`previous_close`/`price` DECIMAL(20,6) |
| `fundamental_snapshot` | `(instrument_id, trade_date)` 复合主键 | 去掉 id 与唯一约束（PK 即唯一）；补外键；三指标 DECIMAL(20,6)；`source` 可空 |
| `trading_calendar` | `(market, trade_date)` 复合主键 | 去掉 id 与唯一约束 |
| `job_status` | `job_name` | 结构不变；时间列 TIMESTAMPTZ；`last_duration_ms` BIGINT |
| `app_setting` | `key` | `value` 可空（方案 §14；现状 NOT NULL，当前无任何读写方，无行为影响） |

通用规则：

- **删除顺序由 Service 显式保证**：删自选 = 同一写锁内两段提交，先 `DELETE watchlist_tag WHERE instrument_id=...` 并提交、再 `DELETE watchlist` 并提交（替代现状的 ON DELETE CASCADE，见 research `watchlist_service.py:115-120`；同事务先删子表会被 DuckDB 1.5.5 误拦，见 Open Questions 第 8 条）；删标签仍是"先 `count_usage` 检查、被引用则 409 拒绝"（`TagInUseError`），数据库 RESTRICT 外键作为兜底。
- **时间列全部 `TIMESTAMPTZ`**（SQLAlchemy `DateTime(timezone=True)`），交易日仍是 `DATE`（见 D4）。
- 外键全部命名（`fk_<table>_<col>`），小业务表正常使用 PK/UNIQUE/FK 约束；不建多余索引（PK 隐含索引，`tag.name` 唯一约束即可）。
- 命名规范沿用方案 §38：`instrument_id`/`trade_date`/`source`/`created_at`/`updated_at`/`fetched_at`，表名 snake_case，单数表名与现状一致。

### D4. 时间语义统一：全库 aware，落库不丢时区

现状的三种形态（job_status naive 北京时间、quote_snapshot aware 写 naive 读、`_iso_beijing` 双形态兼容）统一为：**所有 `DateTime(timezone=True)` 列存 aware datetime（`now_beijing()` 产出的 aware 值直接写入），DuckDB TIMESTAMPTZ 保真往返，读出即 aware**。

- `job_status_service._now()` 删除 naive 化处理（`replace(tzinfo=None)`），直接存 aware 北京时间。
- `/api/admin/status` 的 `_iso_beijing` 保留输出契约（北京时间 ISO 带 +08:00），但内部简化为单一 aware 分支——对外输出格式不变。
- `quote_cache.is_stale` 不再需要"naive 按宿主时区解释"的兼容分支。
- 页面展示时区仍统一 `Asia/Shanghai`（前端 `fmtTime` 行为不变）。

### D5. `app/db.py` 重写：通用化 + 写协调器

新 `db.py` 职责（方案 §36）：Base、`create_db_engine`、`make_session_factory`、`check_database`、`init_db`（仅测试用，create_all）+ **`WriteCoordinator`**。

- `create_db_engine(url)`：`duckdb:///` 前缀解析出文件路径 → 确保父目录存在（通用化 `_ensure_sqlite_dir`，排除 `:memory:`）→ `create_engine(url)`，**无 connect_args、无事件钩子、无 PRAGMA**。
- sessionmaker 维持 `autoflush=False, expire_on_commit=False`（全架构依赖此约定：显式 flush + commit 后对象可继续读属性）。
- **`WriteCoordinator`**（新增，方案 §6.1）：进程级 `threading.RLock` + `with_retry(retries=3, backoff 短暂)`。背景：DuckDB 乐观并发下，**同表并发写事务会冲突**（粒度待 Phase 0 验证，按最坏的表级冲突设计）。现状写路径经梳理为：quote_snapshot（RefreshService tick 与 admin 手动刷新可能并发）、fundamental_snapshot（Job `_persist` 与添加自选的即时估值刷新可能并发）、job_status（两个 Job 同时更新）、trading_calendar（日历拉取）、watchlist/instrument/watchlist_tag（API 请求）。**所有写事务统一在 WriteCoordinator 的锁内提交**——单进程 + 个人工具的写频（60 秒一批 upsert、交互式 CRUD）下序列化开销可忽略，换来确定性。
- 锁的粒度是"整个写事务"而非单条语句；读路径不加锁（DuckDB MVCC 读不阻塞写）。

### D6. Repository 适配：原子 upsert + 显式删除

- **`QuoteSnapshotRepository.upsert` 改为原子 upsert**：首选 SQLAlchemy Core `insert(QuoteSnapshot).on_conflict_do_update(index_elements=[instrument_id], ...)`（duckdb-sqlalchemy 基于 PostgreSQL 方言，预期支持；Phase 0 验证）。**回退方案**：维持 SELECT-then-UPDATE 模式，但在 D5 写锁内执行——PK 保证无重复行，语义与现状一致。删除 `latest`/`latest_many` 中的内存择新逻辑（PK 即一行，`latest_many` 退化为 `WHERE instrument_id IN (...)` 直查）。
- `FundamentalRepository.upsert` 同策略（复合主键保护幂等）；`InstrumentRepository.upsert` 语义不变（get → insert/部分更新，PK 查询）。
- **`WatchlistService.remove`**：写锁内两段提交——先删 `watchlist_tag` 关联行并提交，再删 `watchlist` 行并提交（替代级联；同事务先删子表被 DuckDB 1.5.5 FK 检查误拦，见 Open Questions 第 8 条）。
- **`WatchlistService.set_tags`**：全量替换语义不变，`sa_delete` 的 where 从 `watchlist_id == row.id` 改为 `instrument_id == ...`，插入 `WatchlistTag(instrument_id, tag_id)`。
- **排序键变化**：`list_ordered` / `list_ordered_with_tags` 的 `ORDER BY (sort_order, id)` 改为 **`ORDER BY (sort_order, created_at)`**；Python 分组键从 `row.id` 改为 `instrument_id`。`next_sort_order` 的 +10 间隔策略不变（间隔保证 sort_order 实际不重复，tie-break 几乎不触发）。
- `TradingCalendarRepository.save_days`：commit 从仓储层上移到调用方（对齐"commit 在 Service/Job 层"的全局约定，消除隐式提交同 Session 其它变更的风险）；幂等改为复合主键 + 写锁内 SELECT-then-INSERT（或 ON CONFLICT DO NOTHING，Phase 0 定）。
- 事务边界约定固化：Repository 只 flush，Service/Job/调用方 commit；写事务包 `write_coordinator.write()` 上下文。

### D7. 并发与部署：单进程、显式单 worker

- Dockerfile CMD 改为显式 `sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"`——迁移失败容器退出（现状行为），worker 数写死在命令里而非依赖默认值。
- compose 保持"一个应用容器 + `./data:/app/data` 卷 + `config.yaml:ro`"，服务/镜像名改 `marketmind`，数据库文件 `data/marketmind.duckdb`（`.wal` 同目录）。DuckDB 是嵌入式库，**不加**数据库容器。
- 后台任务模型不变：lifespan 内 asyncio Task + `asyncio.to_thread`，与请求共享 Engine、各开短 Session；lifespan 不做 create_all、不跑 alembic。
- `reference/`（上游参考克隆）与 `research/`（分析报告）进 `.gitignore`，不进新仓库；`.gitignore` 在现状基础上增加 `*.duckdb`、`*.duckdb.wal`、`*.db`、`*.db-wal`、`backups/`、`*.parquet`、`reference/`、`research/`。

### D8. 项目命名与骨架

- 项目名 **marketmind**；包结构沿用方案 §35：根目录 `app/`（main/config/version/db + api/models/schemas/providers/observability/repositories/services/jobs/templates/static）、`alembic/`、`scripts/`（本里程碑只建目录占位）、`tests/`（unit/ + integration/，migrations 场景并入 integration 或独立子目录由实现定）。
- **移植方式 = 复制后修改**：从 `reference/stocksview` 拷贝文件再按本设计逐点改造，保持文件划分、函数签名、异常类型、文案逐字不变—— diffs 可审、行为可对照。禁止"顺手重构"。
- 版本从 **v0.1.0** 重新起步（`app/version.py` 的 `APP_VERSION` 与 pyproject `version = "0.1.0"` 同步维护；应用版本与数据库 revision 不强制对应，方案 §44）。
- `config.example.yaml` 的 `database.url` 示例改为 `duckdb:///./data/marketmind.duckdb`（`DatabaseConfig.url` 默认值同步）；`database.backup_before_migrate`/`backup_keep` 配置项**本里程碑不加**（属 T12）。
- README 重写为 marketmind 版（保留 stocksview README 的结构：快速开始、配置说明、健康检查、升级思路），CHANGELOG 从 v0.1.0 重新记。

### D9. 测试策略：真实 DuckDB 文件 + 共享 conftest

- **禁止**用 SQLite 内存库代替 DuckDB（方案 §46.2）；unit/integration 的 DB fixture 统一用 `tmp_path` 下临时 `.duckdb` 文件，由新增 `tests/conftest.py` 提供（`duckdb_engine`/`duckdb_session_factory`/`duckdb_app` 等共享 fixture，消除现状 12+ 处复制粘贴）。
- 18 个测试文件全部移植并适配：`check_same_thread` 删除；`sqlite_master` 查询改 `information_schema.tables`（或 `duckdb_tables()`）；batch 约束断言删除；PRAGMA 外键兜底用例改为验证 DuckDB FK RESTRICT；naive 北京时间断言改为 aware 断言；测试名/注释中的 SQLite 措辞更新（如 `test_snapshot_saved_to_sqlite` → `test_snapshot_saved_to_duckdb`）。
- `test_migrations.py` 语义完整移植为 DuckDB 版（方案 §46.3/46.5）：① 空目录 `alembic upgrade head` 全链建库 + 表集合断言；② **防漂移测试**——migration 产物与 `Base.metadata.create_all` 逐表比对（列名/类型/可空/索引/约束）；③ migration 失败不启动应用（容器 CMD 语义，用 programmatic API 模拟）；④ FK 行为验证。v0.02/v0.03 数据搬迁用例**不移植**（历史链已废弃），旧 revision → head 升级测试属 T13。
- 新增 Repository 集成测试覆盖 DuckDB 特性面：复合主键、sequence 生成 tag_id、upsert 幂等、FK RESTRICT、TIMESTAMPTZ 往返、并发写（两个线程同表写事务，验证 WriteCoordinator 下无冲突异常）。
- 在线冒烟用例保留 `@pytest.mark.online` 标记机制。

### D10. Phase 0 技术验证前置（风险最高的先做）

方案 §51 的 Phase 0 验证清单作为实施的第一组任务（见 tasks.md 第 2 组），产出一次性 spike 脚本 + 保留为集成测试：

1. duckdb-sqlalchemy ORM CRUD / 复合主键 / FK（含 RESTRICT 语义与错误类型）；
2. `CREATE SEQUENCE` + `DEFAULT nextval` 经 Alembic 基线创建；
3. `INSERT ... ON CONFLICT DO UPDATE`（含方言 `on_conflict_do_update` 可用性）；
4. TIMESTAMPTZ aware 写入/读出往返；
5. 同进程多连接同表并发写的事务冲突行为（确认冲突粒度与异常类型 → 校准 WriteCoordinator 与重试策略）；
6. Engine 连接池行为（默认 QueuePool vs NullPool）；
7. 复杂表 rebuild migration（建新表 → 搬数据 → 换名）在 DuckDB 下的事务语义；
8. 后台任务线程 + 请求线程同时访问（TestClient + to_thread）；
9. Docker 单进程运行冒烟。

验证结论回写本设计的"Open Questions"并在实现时定案；任一项验证失败 → 启用对应回退方案（D5/D6 已列）。

## Risks / Trade-offs

- [duckdb-sqlalchemy 是第三方方言，能力边界与 DuckDB 版本强耦合] → 精确锁版本；方言相关代码只允许出现在 `app/db.py` 与 Repository upsert；D10 验证 + 迁移/集成测试作升级门禁。
- [DuckDB 同表并发写冲突（乐观并发 abort）] → D5 WriteCoordinator 序列化全部写事务 + 短事务 + 有限重试；D10-5 实测冲突粒度与异常类型校准实现。
- [DuckDB FK 约束执行行为与 SQLite PRAGMA 语义不同] → 业务层检查（count_usage → 409）是第一道防线并保持现状；D10-1 实测 RESTRICT；若方言层行为异常，以业务层为准并在测试中固化实际语义。
- [ON CONFLICT 方言支持不确定] → D6 双方案：Core on_conflict_do_update 优先，SELECT-then-UPDATE（写锁内）兜底，两者在 PK 下行为等价。
- [排序 tie-break 从 id 改为 created_at，极端情况下顺序与旧库不完全一致] → +10 间隔使 sort_order 实际唯一，tie-break 几乎不触发；specs 的排序要求按 (sort_order, created_at) 固化。
- [时间语义从 naive 转 aware 可能引起序列化细节差异] → `/api/admin/status` 输出契约不变（北京时间 ISO +08:00），集成测试逐字段断言护航；job_status 相关断言改 aware。
- [移植 80 个文件时的行为漂移] → "复制后修改"原则 + 全套测试移植作为验收门禁 + specs 作为兼容契约；review 时 diff 对照参考库。
- [Docker 镜像体积增大（duckdb wheel + pandas 链）] → 接受；python:3.12-slim 不变。
- [单进程写入是容量上限] → 有意选择（方案 §57 第 6 条）；未来并行回测走 Parquet/快照路线，不改 live 库多进程写。
- [quote_snapshot 精度 (10,4)→(12,6) 属schema 变化] → 新库无历史数据，无迁移成本；API 层 float 序列化不受影响。

## Migration Plan

**部署**（全新，无存量）：

1. `git init` marketmind 仓库，按 D8 骨架落地；
2. 本地/CI：`uv pip install -e .`（或 pip）→ `cp config.example.yaml config.yaml` → `alembic upgrade head` → `uvicorn app.main:app --workers 1`；
3. Docker：`docker compose up` —— CMD 自动 `alembic upgrade head && uvicorn --workers 1`，`./data` 卷持久化 `marketmind.duckdb`；
4. 验收：跑通全部移植测试 + Phase 1 验收清单（方案 §52）。

**回滚**：本里程碑只服务全新部署——停容器、删 `data/marketmind.duckdb`（及 `.wal`）即可回空库；旧 stocksview SQLite 部署不受影响、可继续独立运行。

**实施顺序**（对应 tasks.md）：骨架 → Phase 0 验证 → db.py/依赖 → models → baseline → repositories → services/API/UI → Docker → 测试补全。

## Open Questions（已定案：Phase 0 验证结论，2026-09-13）

以下问题由 D10 的 Phase 0 验证任务定案（scripts/spike/ 五个脚本 + alembic 全链冒烟，DuckDB 1.5.5 + duckdb-sqlalchemy 1.5.5.5 + alembic 1.20.0）：

1. **on_conflict_do_update 可用（D6 采用首选方案）**：`postgresql.insert(...).on_conflict_do_update(...)` 单列主键与复合主键均可直接使用；原生 SQL `ON CONFLICT DO UPDATE` / `DO NOTHING` 亦可用。QuoteSnapshot/Fundamental 两个 Repository 维持 Core on_conflict_do_update 实现，无需兜底改写。
2. **写冲突异常与粒度已校准**：不同行并发写不冲突；同一行并发写与"长事务期间另连接提交写"均抛 `OperationalError`（orig 为 `_duckdb.TransactionException` "Conflict on update!"）——WriteCoordinator 的通用 `except Exception` 捕获范围已覆盖，维持现状。写期间并发读正常（MVCC 验证）。
3. **FK RESTRICT 生效**：删除被引用父行抛 `IntegrityError`（orig 为 `_duckdb.ConstraintException`）；无引用时可正常删除。DB 层兜底测试按 `pytest.raises(IntegrityError)` 断言。
4. **默认连接池为 QueuePool（大小 5）**：4 线程并发 checkout 无异常；复杂表 rebuild（建新表→搬数据→换名）事务原子、失败回滚干净、事务内 CREATE/DROP TABLE 可用。生产维持默认 QueuePool，不显式 NullPool。
5. **sequence 路线定案（原生 DDL）**：alembic 1.20 的 Operations 无 `create_sequence`，且 `Column(..., Sequence(...))` 在 create_table 中不渲染 DEFAULT。基线迁移采用 `op.execute("CREATE SEQUENCE seq_tag_id START 1")` + 列显式 `server_default=nextval('seq_tag_id')`；模型侧 `Sequence("seq_tag_id")` 在 INSERT 时由 SQLAlchemy 主动取号（create_all 产物无 DEFAULT 亦正常取号，递增已验证）。DDL DEFAULT 与应用取号共用同一 sequence，行为一致。
6. **（新增）duckdb-sqlalchemy 无内建 Alembic 支持**：`MigrationContext.configure` 对 duckdb 方言 KeyError。已在 alembic/env.py 注册 `DuckDBImpl(DefaultImpl)`（`__dialect__ = "duckdb"`、transactional_ddl=True），全链 `upgrade head` → `downgrade base` → 复升冒烟通过。
7. **（新增，移植期实测）DuckDB 1.5.5 FK bug：父表被 FK 引用时其 UNIQUE 列不可 UPDATE**——`UPDATE tag SET name=... WHERE tag_id=1`（tag 被 watchlist_tag 引用）被误拦为 FK violation（"key tag_id: 1 is still referenced"），即使未改主键；非 UNIQUE 列 UPDATE 正常、DELETE 拦截正常、父表无 UNIQUE 约束时全部正常。**定案：tag.name 不设数据库 UNIQUE**（模型与基线迁移已移除 uq_tag_name），全库唯一由 TagService 写锁内 SELECT 查重保证（单写者模型无并发窗口）；spec 的"全库唯一"需求由 Service 层满足，行为不变（重复名称仍 409）。
8. **（新增，移植期实测）DuckDB 1.5.5 FK 检查看不到同事务内已删的子表行**——同一事务内"先 DELETE watchlist_tag 再 DELETE watchlist"被误拦（子表删除对本事务后续语句的 FK 检查不可见）；分两次提交则正常（同事务 INSERT 父+子正常）。**定案：`WatchlistService.remove` 改为写锁内两段提交**（先删关联并 commit，再删条目并 commit），写锁保证两段之间无其他写者；原子性损失（第一段成功第二段失败时关联已删、条目仍在）在单写者 + 删除幂等可重试的个人工具场景下可接受。design D5 的"同事务显式删除"表述据此修正。
9. **（新增，移植期实测）DuckDB 严格执行 SQL 标准 GROUP BY**——`GROUP BY tag_id` 而 SELECT 含 name 等非聚合列时报 Binder Error（SQLite/MySQL 的裸列容忍不可用）。`TagRepository.list_with_usage` 改为 `group_by(Tag)`（展开全部列），tag_id 为主键语义不变。
10. **（新增，移植期实测）DECIMAL 列读回 Decimal**（SQLite 时代返回 float）：非 2 的幂次分母的小数（如 2.6）`Decimal == float` 比较为 False，测试断言须用 `Decimal` 字面量；`information_schema.columns.data_type` 返回带精度后缀的 `DECIMAL(20,6)`（精确标度以 numeric_precision/numeric_scale 为准）；`duckdb_sequences()` 列名为 `schema_name`。
