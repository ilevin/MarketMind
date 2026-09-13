# duckdb-migration 实施任务清单

> 对应技术方案第一个里程碑 T01–T10；实施顺序按依赖排列：骨架 → Phase 0 验证 → 连接层 → 模型 → 基线迁移 → 仓储 → 服务/API → Docker → 测试 → 验收。
> 移植原则（design.md D8）：从 `reference/stocksview` **复制后修改**，保持文件划分、函数签名、异常类型与文案不变，禁止顺手重构。

## 1. 项目骨架与依赖（T01/T02）

- [x] 1.1 在 marketmind 根目录 `git init`，编写 `.gitignore`：沿用 stocksview 条目，新增 `data/`、`*.duckdb`、`*.duckdb.wal`、`*.db`、`*.db-wal`、`backups/`、`*.parquet`、`reference/`、`research/`
- [x] 1.2 编写 `pyproject.toml`：项目名 marketmind、version 0.1.0、requires-python >=3.11；依赖沿用 stocksview（fastapi/uvicorn/jinja2/SQLAlchemy/pydantic/pyyaml/httpx/akshare/tushare/alembic），新增精确锁定版本 `duckdb==1.5.5`、`duckdb-sqlalchemy==1.5.5.5`；dev 依赖 pytest、pytest-asyncio
- [x] 1.3 从 `reference/stocksview` 复制 `app/`（含 templates/static）、`alembic/script.py.mako`、`alembic.ini`、`Dockerfile`、`docker-compose.yml`、`config.example.yaml`；`app/version.py` 改为 `APP_VERSION = "v0.1.0"`；模板页脚与全部品牌硬编码处 `StocksView` 改为 `marketmind`（对照 `research/stocksview-api-ui.md`，仅改品牌文案，不动交互与数据结构）
- [x] 1.4 `app/config.py` 的 `DatabaseConfig.url` 默认值与 `config.example.yaml` 示例改为 `duckdb:///./data/marketmind.duckdb`
- [x] 1.5 用 uv 创建虚拟环境并安装（`uv venv && uv pip install -e ".[dev]"`），冒烟验证 `create_engine("duckdb:///:memory:")` 可创建引擎（方言注册成功）
- [x] 1.6 重写 `README.md`（marketmind 快速开始/配置说明/健康检查/部署）与新建 `CHANGELOG.md`（v0.1.0 条目）

## 2. Phase 0：DuckDB 技术验证（design D10，先于全部改造）

- [x] 2.1 编写验证脚本（`scripts/spike/` 或临时测试）：duckdb-sqlalchemy ORM CRUD、复合主键、TIMESTAMPTZ aware 写入/读出往返
- [x] 2.2 验证 `CREATE SEQUENCE` + `DEFAULT nextval('seq_tag_id')` 与外键行为（RESTRICT 语义、违反时的异常类型）
- [x] 2.3 验证 `INSERT ... ON CONFLICT DO UPDATE`（含 SQLAlchemy 方言 `on_conflict_do_update` 可用性）；不可用则记录结论并确定采用写锁内 SELECT-then-UPDATE 兜底
- [x] 2.4 验证同进程多连接并发写：两线程并发写同表（不同行/同行），记录冲突异常类型与冲突粒度 → 确定 WriteCoordinator 重试的捕获范围
- [x] 2.5 验证 Engine 默认连接池行为（决定是否显式 NullPool）与复杂表 rebuild migration（建新表→搬数据→换名）的事务语义
- [x] 2.6 把全部验证结论回写 `design.md` 的 Open Questions 定案；可长期保留的验证固化为集成测试（并入 9.5）

## 3. 数据库连接层重写（T03）

- [x] 3.1 重写 `app/db.py` 的 `create_db_engine`：解析 `duckdb:///` URL、自动确保数据库文件父目录存在（排除 `:memory:`）、无 connect_args、无事件钩子、无 PRAGMA；`check_database` 保持 `SELECT 1` 通用实现
- [x] 3.2 实现 `WriteCoordinator`：进程级 `threading.RLock` + `with_retry`（1–3 次、短暂退避），提供写事务上下文；挂为模块级单例供各写路径使用
- [x] 3.3 `make_session_factory` 维持 `autoflush=False, expire_on_commit=False`；`init_db` 保留并 docstring 标注仅测试使用
- [x] 3.4 冒烟：临时 `.duckdb` 文件上建 engine + session + `check_database` 通过

## 4. Core Models 重做（T04）

- [x] 4.1 `instrument`：`instrument_id` 为主键（删除自增 id 与唯一索引），`exchange`/`currency` 可空，`created_at`/`updated_at` 改 `DateTime(timezone=True)`
- [x] 4.2 `watchlist` / `index_watchlist`：`instrument_id` 为主键 + 命名外键 `fk_*_instrument`，`sort_order` 加 server_default 0，删除 id 与 `uq_*` 唯一约束
- [x] 4.3 `tag`：`tag_id` 以显式 `Sequence('seq_tag_id')` 生成；`watchlist_tag`：复合主键 `(instrument_id, tag_id)`、两个命名外键、**不带 ondelete**、删除 id
- [x] 4.4 `quote_snapshot`：`instrument_id` 为主键 + 外键；`change_percent`/`volume_ratio` DECIMAL(12,6)、`price`/`previous_close` DECIMAL(20,6)；时间列 TIMESTAMPTZ
- [x] 4.5 `fundamental_snapshot`（复合主键 + 外键 + DECIMAL(20,6)、source 可空）、`trading_calendar`（复合主键）、`job_status`（TIMESTAMPTZ、`last_duration_ms` BigInteger）、`app_setting`（value 可空）
- [x] 4.6 更新全部模型 docstring：清除 SQLite/PRAGMA 字样（`trading_calendar.py`、`watchlist_tag.py` 等，对照 research 耦合清单）

## 5. Alembic 基线迁移（T05）

- [x] 5.1 `alembic/env.py` 删除离线/在线两处 `render_as_batch=True`，URL 同源逻辑与 `target_metadata` 不动
- [x] 5.2 编写 `0001_duckdb_baseline`：一次创建 10 张表 + `seq_tag_id` + 全部命名外键/唯一约束，与模型逐列一致；downgrade 为全量 DROP（可回空库）
- [x] 5.3 空目录冒烟：`alembic upgrade head` 后 10 张表与 `alembic_version` 记录齐全，`alembic downgrade base` 可回空库

## 6. Repository 与状态服务适配（T06）

- [x] 6.1 `QuoteSnapshotRepository`：upsert 改原子 upsert（按 2.3 结论选 `on_conflict_do_update` 或写锁内 SELECT-then-UPDATE）；`latest`/`latest_many` 简化为按主键直查（删除内存择新逻辑）
- [x] 6.2 `FundamentalRepository` / `InstrumentRepository`：按复合主键/业务主键适配 upsert，幂等与部分更新语义不变
- [x] 6.3 Watchlist 仓储：排序键改 `(sort_order, created_at)`、Python 分组键改 `instrument_id`；`set_tags` 的 delete/insert 改按 `instrument_id`；`next_sort_order` +10 间隔策略不变
- [x] 6.4 `TradingCalendarRepository.save_days`：commit 上移到调用方；幂等按复合主键（写事务经 WriteCoordinator）
- [x] 6.5 `JobStatusService`：`_now()` 改返回 aware 北京时间（删除 naive 化处理）；三态更新与吞异常语义不变

## 7. Service / API / UI 打通（T07）

- [x] 7.1 `WatchlistService.remove`：同一事务内显式删除 `watchlist_tag` 关联行再删除条目（替代 ON DELETE CASCADE，对外行为不变）
- [x] 7.2 `app/api/status.py` 的 `_iso_beijing` 简化为单一 aware 分支（输出格式不变）；`quote_cache.is_stale` 删除 naive 兼容分支
- [x] 7.3 `refresh_service` / `fundamental_refresh` 的写事务（`_refresh_instrument_list`、`_persist`）包入 WriteCoordinator；行为与失败语义不变
- [x] 7.4 API 层逐文件核对（quotes/watchlist/index_watchlist/tags/admin/status）：响应 JSON 结构、错误码、中文文案逐项对照 `research/stocksview-api-ui.md` 契约清单不变
- [x] 7.5 清理全部代码注释/docstring 中的 SQLite 字样（`api/quotes.py`、`providers/trading_calendar/provider.py`、`services/quote_cache.py` 等）
- [x] 7.6 本地冒烟：起服务后三个页面 + 12 个 API 端点逐个走通（含添加自选即时刷新、标签关联、排序）

## 8. Docker 单 worker 部署（T08）

- [x] 8.1 `Dockerfile` CMD 显式 `--workers 1`；确认 `alembic.ini`/`alembic/` COPY 与 `/app/data` 目录创建保留
- [x] 8.2 `docker-compose.yml` 服务名/镜像名改 marketmind；`./data` 卷与 `config.yaml:ro` 挂载不变
- [x] 8.3 `docker compose build && docker compose up` 冒烟：全新卷自动建库（alembic upgrade head）、`/health` 200、页面可访问、重启后数据不丢

## 9. 测试体系（T09）

- [x] 9.1 新建 `tests/conftest.py`：提供 DuckDB 临时文件 fixture（engine / session_factory / TestClient app 工厂），消除 12+ 处复制粘贴
- [x] 9.2 移植 12 个 unit 测试：SQLite 内存库/check_same_thread 全部替换为 conftest fixture；`test_job_status_service` 时间断言改 aware；`test_tag_service` 外键兜底用例按 Phase 0 实测行为改写；测试名/注释中 SQLite 字样更新
- [x] 9.3 移植 6 个 integration 测试：临时文件库 URL 改 `.duckdb`；`sqlite_master` 查询改 `information_schema.tables`；batch 约束断言删除；全部响应字段级断言保持
- [x] 9.4 重写 `test_migrations.py`（DuckDB 版）：空库全链建库断言、防漂移比对（migration 产物 vs `create_all` 产物逐表列/类型/约束）、迁移失败应用不启动、FK RESTRICT 行为；v0.02/v0.03 数据搬迁用例不移植（历史链废弃）
- [x] 9.5 新增 DuckDB 特性集成测试：复合主键、sequence 生成 tag_id、upsert 幂等、TIMESTAMPTZ 往返、并发写（WriteCoordinator 序列化下无冲突异常）
- [x] 9.6 全量 `pytest` 通过（`online` 标记默认跳过），无跳过的失败用例

## 10. 验收与收尾（T10）

- [x] 10.1 对照技术方案 §52 Phase 1 验收清单逐项勾验（20 项，含"全新 docker compose up 自动建库、无 SQLite 依赖、无 PRAGMA、无 batch、单 worker、重启数据不丢、quote_snapshot 一证券一行"等）
- [x] 10.2 对照本变更 `specs/` 16 个能力规格的 Scenario 抽查关键契约（API 响应结构、错误码、时间格式、缓存回退、删除保护）
- [x] 10.3 `grep -ri sqlite` 全库确认无残留（`reference/`、`research/`、`openspec/` 归档除外）
- [ ] 10.4 整理 git 提交（按任务组拆分），CHANGELOG 补记 v0.1.0 发布说明，准备进入第二里程碑（T11–T16 升级工具链）
