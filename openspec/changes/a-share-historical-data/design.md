## Context

MarketMind 是 FastAPI + DuckDB + Jinja2 单体应用（uvicorn 固定 `--workers 1`，WriteCoordinator 串行化全部写事务），v0.2.0 已具备多用户认证、自选/标签/指数隔离、实时行情（AKShare/腾讯）与自选估值（Tushare `daily_basic` serving cache）。数据库迁移当前 head 为 `0002_multi_user_auth`。

本变更依据两份已确认的上游文档实施，它们是需求的权威来源，本设计不重复其全文，只做工程落地映射与补充决策：

- 《docs/MarketMind_历史行情数据产品设计与数据说明.md》v0.1（产品边界与验收）
- 《docs/MarketMind_A股历史数据技术方案_v0.2.md》v0.2（表结构、算法、Phase 0~8 实施顺序、D1~D29 决策表、§76 实施硬约束）

核心工程不变量（全部产物与任务必须服从）：四个日级数据集（daily/adj_factor/daily_basic/moneyflow）各自拥有独立连续水位线；某数据集某交易日未完整成功，该数据集绝不能越过该日期推进；网络请求永远在 WriteCoordinator 写锁之外；历史数据能力扩展并复用现有 Provider 框架，不建第二套。

## Goals / Non-Goals

**Goals:**

- 从 2010-01-01 建立 A 股全市场原始事实数据底座（4 日级事实集 + 4 主档集），字段完整、单位与 NULL 语义保真
- 连续水位线同步引擎：严格交易日推进、失败不跳日、10 次退避重试、单日原子提交、幂等、可恢复
- 统一同步入口（定时 20:30 / startup catch-up / 管理员手动共用），single-flight 互斥
- 管理员数据控制台 `/admin/data`（状态可见、进度可轮询、手动可触发）
- 与现有功能零冲突并存（FundamentalRefreshJob、实时行情、多用户隔离不受影响）

**Non-Goals:**

- 分钟/Tick/逐笔数据、ETF/指数历史、财务报表库
- 回测引擎、因子/技术指标预计算、qfq/hfq 事实表（复权按需计算）
- 公开历史查询 API（`/api/history/*`）、管理员手工编辑历史数据或输入水位
- 新基础设施（Redis/Celery/消息队列/第二数据库/前端框架/多 worker）
- 淘汰或改造现有 `fundamental_snapshot` / FundamentalRefreshJob

## Decisions

以下决策编号引用技术方案 D1~D29；实现文件结构以技术方案 §58 为基准，允许按当前仓库实际命名微调，职责不得混乱。

### 1. 数据分层与表设计（D1、D8~D11、D21~D23）

新增 11 张表：主档 `cn_stock_basic` / `cn_stock_company` / `cn_stock_name_change`（小表，ORM，可有 PK/FK）；事实 `market_daily_bar` / `market_adj_factor` / `market_daily_basic` / `market_moneyflow`（千万行级，SQLAlchemy Core `Table` 定义，无物理 PK/FK/二级索引，业务唯一键 `(instrument_id, trade_date)` 由整日替换 + ledger 保证）；同步控制 `history_sync_state` / `history_day_status` / `history_sync_run` / `history_sync_run_dataset`。`trading_calendar` 只增 4 个可空列（exchange/pretrade_date/source/fetched_at），不新建日历表。

- 为什么事实表不用 ORM/索引：DuckDB 价值在列式批量扫描；数据按 trade_date 顺序追加；避免千万行 ORM identity 与索引维护成本。单股 K 线等读取场景上线后再按真实 benchmark 决定索引/物化层。
- 为什么状态按 dataset 而非 per-instrument：获取模型是"按交易日取全市场"，dataset+trade_date 状态量小（4×约 4000 行）、易证明连续、避免数千万状态行。
- `fundamental_snapshot` 保持 serving cache 定位，与 `market_daily_basic` 允许字段重叠，未来切换读取源是独立重构（D11）。

### 2. 双层水位模型：state + day ledger

`history_sync_state.latest_complete_trade_date` 提供 O(1) 的"下次从哪开始"；`history_day_status`（(dataset, trade_date) PK，仅记 COMPLETE）是可验证的连续性账本，支撑崩溃恢复、管理员追查与 reconcile。水位含义是"从 2010 起所有应处理交易日连续完成到该日"，`MAX(trade_date)` 只能用于发现矛盾（D3）。首次初始化不造 2009-12-31 之类虚假水位，水位为 NULL 时由 Planner 取第一个 ≥ 2010-01-01 的 open day。

### 3. 严格日历推进与 reconcile

待处理列表来自 `trading_calendar`（market='CN' AND source='tushare' AND is_open）区间升序查询，禁止 `date+1`/工作日近似（D12）。每次任务开始对四数据集轻量 reconcile；发现 state/ledger 不一致时保守回退到第一个缺失 COMPLETE 日的前一交易日并重同步，绝不依更晚事实日期跳缺口。日历按年份获取（2010→当前年末），当前年度缺失或来源非严格即重拉。

### 4. 单日原子事务（D5~D8）

事务边界固定为"一个数据集的一天"：请求→normalize→校验（锁外）→ `with write_coordinator.write():` → 锁内 `with session_factory() as session:` 单事务完成 [查 old_count → DELETE 当日 → 批量 INSERT → upsert day_status → update state（水位/record_count/min-max）→ update run_dataset] → `session.commit()` → 出锁（范例 app/services/refresh_service.py；write() 上下文管理器是现有唯一在用的写锁 API，with_retry() 要求 fn 整体可重放且当前无调用方，本变更不引入）。中途任何异常整体回滚，数据库不会出现"水位到了 D 但 D 数据半写"或"D 整天空洞"。整日 DELETE+INSERT 而非逐行 UPSERT：幂等易证明、吸收上游修订（上游删改行不会留旧行）、与"整天成功才推进"语义一致。现有行情/自选写路径在回填的网络间隙正常拿锁（D7）。

### 5. Provider 框架复用（D26~D28）

在现有 `app/providers/base.py` 增加历史内部标准模型 + `ProviderBatch[T]` + `HistoricalMarketDataProvider` Protocol；新增 `app/providers/history/__init__.py`（HistoryProviderRegistry，仿 QuoteProviderRegistry：AppConfig 选源、单例、`call_with_metrics`）与 `app/providers/history/tushare.py`（TushareHistoricalMarketDataProvider）。Tushare DataFrame/SDK 对象与原始字段名不越过 Provider 边界。不新建 `providers/history/base.py`、不建历史专属 metrics/timeout。交易日历例外：扩展现有 `TushareTradingCalendarProvider` 增加 strict 范围模式（strict=True 无任何 fallback），HistorySyncService 只用 strict（D29）；不复用 `TushareFundamentalProvider`（契约不匹配：吞异常降级 vs 失败必须传播影响水位）。

### 6. 共享 Tushare transport 与限流（D13）

新增 `app/providers/tushare_common.py`：`create_tushare_pro_client(config)` + `TushareRequestGate`（threading.Lock + time.monotonic()，进程级，按 endpoint 最小间隔）。三个 Tushare Provider（fundamental/calendar/history）渐进共享，改造不改旧契约。默认间隔 0.6s（约 100 次/分），stock_basic ≥1.25s/请求；2000 积分下不做高并发。调用层级固定：`call_with_metrics`（方法级指标）→ Provider（fields/校验/normalize）→ RequestGate（节奏）→ SDK，三者不可互相替代。

**超时归属（v0.3.0 实测修正，补充 §27/§30.1——不新增 D 编号）**：`call_with_metrics` 对历史 Provider 方法不设方法级 wall-clock timeout。一个方法可能含多个受 gate 限流的真实请求——`stock_basic` 15 分片 × 1.25s ≈ 17.5s > 15s，固定上限下**必然**假超时（真实环境已复现：`超时: timeout=15.0s duration=15001ms`），且 `ThreadPoolExecutor(shutdown(wait=False))` 会留下仍在发请求的线程与重试重叠。修正为：

- 单请求网络超时固定在共享 transport / 原生 SDK 请求层：`ts.pro_api(token, timeout=config.providers.timeout.tushare)`，SDK 在 `DataApi.query` 内以 `requests.post(..., timeout=T)` 对每次调用只发的那一个 HTTP 请求生效。**不能按调用传 `timeout=`**：`DataApi.__getattr__` 返回 `partial(self.query, api_name)`，会把 `timeout` 当接口参数塞进请求体；
- gate 只负责节流，先 `acquire()` 再发请求，等待 gate 的时间不计入网络超时；
- 真实请求超时归一化为 `TushareTimeoutError`（同时是 `TimeoutError` 子类）→ 仍进 `timeout_count`，不退化为 `error_count`；`call_with_metrics` 的超时分类只看异常类型（含 `requests.exceptions.ReadTimeout` 这类**不是**内建 `TimeoutError` 子类的真实超时）；
- 方法级只记录 success/error/duration，方法在调用方线程同步执行，不做线程池限时。

### 7. AvailabilityPolicy 与调度（D19~D20）

每个日级数据集经 AvailabilityPolicy 计算 `latest_expected_trade_date`：输入当前 Asia/Shanghai 时间（复用 `BUSINESS_TZ_NAME`，不新增 timezone 配置）+ 严格日历 + 数据集 cutoff（adj_factor 09:30 / daily 16:30 / daily_basic 17:30 / moneyflow 20:30，集中在 config `history.availability`）。统一 Job 默认每天 20:30 执行（含周末——不产生虚假交易日且可追平周五缺口）；`startup_catchup=true` 时启动发现落后即触发 STARTUP 同步。盘中手动触发把目标定在"最近一个已过发布时间的 open day"，不误判失败。

### 8. 重试与错误分类

单 dataset×单交易日最多 10 次尝试；退避 `min(5×2^(n-1), 300)` 秒 × jitter(0.8~1.2)，参数全部可配置。可重试错误（超时/限流/临时服务错误/空结果/解析失败/截断 fallback 失败）走重试；配置类错误（Token 缺失/权限拒绝/schema 不匹配/字段映射错误）快速失败。错误码按技术方案 §51.3 标准化（TUSHARE_*、EMPTY_RESULT、TRUNCATION_RISK、SCHEMA_MISMATCH、DUPLICATE_KEY、TRADE_DATE_MISMATCH、UNKNOWN_INSTRUMENT、INVALID_VALUE、CALENDAR_UNAVAILABLE、DATABASE_ERROR、INTERNAL_ERROR 等 15 个），错误文本过滤 Token 与敏感配置。DB 事务失败可有限重试，但以"单日 10 次同步尝试"为上层边界，不与 WriteCoordinator 自身重试嵌套成无界。同步日志按技术方案 §62/§63 落实：结构化字段 run_id/dataset/trade_date/attempt/row_count/elapsed_ms/error_code；正常完成 INFO、重试 WARNING、10 次失败 ERROR；回填期间每交易日一条概要、不逐行打印、不含 Token。

### 9. 截断防护与空结果（D14~D16）

`daily`/`daily_basic`/`moneyflow` 返回恰 6000 行即 TRUNCATION_RISK，不直接提交；fallback 从 `cn_stock_basic` 取当日证券集做更细粒度请求、合并去重、复检。文档未明确保证多代码参数的接口不猜测参数能力（先保守逐证券，在线 smoke test 确认后再优化 batch）。

**在线实测后的分接口策略（2026-09-19）**：`daily`/`moneyflow`/`adj_factor` 支持逗号分隔多 `ts_code`；**`daily_basic` 会静默返回 0 行**（不报错，危险：把"没查到"伪装成"查过了"，复用 multi-code fallback 会让缺失交易日被判成正常完成、水位照常推进且无错误码暴露）。因此 `daily_basic` 补齐按**候选集 − 已返回代码**逐只查询：正常仍按 `trade_date` 取；仅当返回恰 6000 行时，由 `cn_stock_basic` 的 `list_date`/`delist_date` 生成当日候选集，只对缺失证券逐只查询并合并；每个缺失证券必须得到"有记录"或**明确空结果**（停牌等自然缺失允许为空），任一请求**异常**则该交易日不 COMPLETE、水位不推进；不依赖文档未声明的 `offset`/`limit` 分页。`stock_basic` 主动按 exchange×list_status 分片（3×5）、`stock_company` 按 exchange 分片（上限 4500），任一非空分片命中上限则本轮失败。空结果：历史日期按 EMPTY_RESULT 错误码重试至失败；当日临近发布时间为 WAITING_SOURCE 不推进不报错。证券级自然缺失（停牌无 daily、moneyflow 不覆盖、字段 NULL）不等于日期级缺口，不以"当日上市证券数"校验 daily。

### 10. 统一入口、互斥与恢复（D17~D18）

`HistorySyncService.run(trigger, requested_by)` 是唯一业务入口：recover_stale_runs → ensure_master_prerequisites（trade_cal/stock_basic 硬前置；company/namechange 非阻塞）→ reconcile_daily_watermarks → 顺序执行四日级数据集（互不阻塞，失败不回滚他人）→ finalize_run（SUCCESS/PARTIAL/FAILED/NOOP）。Job 层（`app/jobs/history_sync.py`）持有进程级 single-flight：运行中定时触发记 skip、手动触发 409。执行经 `asyncio.to_thread` 跑同步 Service（与现有 Job 一致，避免阻塞 event loop）。Service 接受 `threading.Event` cancellation：交易日开始前/重试 sleep 前后/master 分片间检查，停机时当前事务允许完成。启动恢复：遗留 RUNNING run 标 INTERRUPTED，SYNCING/RETRYING/CHECKING state 恢复为 LAGGING/CAUGHT_UP，完成依据始终是 ledger+水位。

### 11. 主档策略

stock_basic：24h 过期即刷新，分片全状态获取（含退市），全部 shard 内存校验后单事务 upsert instrument + cn_stock_basic + 状态，不因退市删除。namechange：首次 bootstrap 按 ts_code 排序逐只获取（master_cursor 可中断续跑），之后每 7 天窗口增量（start=上次成功-7天，重叠窗口替换、保留更早历史）。stock_company：每 7 天按 exchange 分片。主档进度用"最近成功刷新/bootstrap_complete/master_cursor"语义，不造交易日水位（D22）。

### 12. Admin API 与页面

新增 `app/api/admin_history.py`（`/api/admin/history-data/{summary,sync,runs,runs/{id}}`，复用现有管理员认证依赖与 CSRF；POST 202/409，不让 HTTP 等待回填完成；requested_by 服务端取）。新增 `app/templates/admin_data.html` + 原生 JS（`initAdminDataPage()` 或独立 admin_data.js，无构建系统；运行中 3~5s 轮询、结束即停）。summary/overall_status 只读 sync 小表（RUNNING > ERROR(核心 FAILED) > LAGGING > WAITING > HEALTHY；company/namechange 失败仅 warning）。`history_sync` 接入 JobStatusService 高层健康，业务进度在 history_sync_* 表（职责分离）。

### 13. 测试策略

默认 `pytest` 全离线：单元（availability 时区/cutoff、planner 顺序与 reconcile、retry 注入 sleep/random、validator 全规则）；集成（真实临时 DuckDB + mock Provider：首次三天同步、中间失败不跳日、下次恢复、数据集独立、事务回滚点、重复运行幂等、6000 fallback、stale run 恢复、single-flight）；迁移测试（0002 状态库升级 0003，旧数据无损）；API 测试（权限/CSRF/409/轮询数据）；性能基准（synthetic 6000 行×100 日，防逐行 ORM 退化）。真实 Tushare 仅 `@pytest.mark.online` smoke（一个 shard、小窗口、一个已完成交易日的四个数据集、一个 exchange 的 company、一只 namechange；验证权限/字段/fields 参数/日期格式/BSE ts_code/上限行为；不写生产库）。

## Risks / Trade-offs

- [首次回填耗时以天计（4 数据集 × 约 4000 交易日 × 0.6s+校验）] → 设计为可跨多次运行续跑（水位即进度）；页面持续展示进度；限流保守避免账号被封；不要求单进程生命周期内跑完。
- [Tushare 接口字段/上限/权限随时间变化] → 显式 fields + 显式 DB schema（新字段须经 migration）；把接口能力变化视为外部依赖变化，online smoke test 守护；错误码可观测。
- [6000 行 fallback 的多代码参数能力未验证] → 默认保守逐证券（慢但正确）；smoke test 确认后再启用 batch 优化；不能确认完整就不推进水位。
- [moneyflow 发布时间无官方承诺] → 保守 20:30 cutoff + WAITING_SOURCE 状态，可配置调整。
- [单日几千行写入的 DuckDB 性能未基准] → 已基准并据此替换 Repository 内部实现（DuckDB staging + INSERT SELECT），不破坏整天原子语义。实测（本机，6000 行）：SQLAlchemy `executemany` 约 2.2 s/6000 行，DuckDB 原生 `executemany` 同量级（tmpfs 上仍 28.6 s，属逐行解析的 CPU 开销而非磁盘）；注册视图 + `INSERT SELECT` 仅约 0.14 s（同机约 14×）。该路径**取自 `session.connection()` 的事务内连接**（不新开连接），DuckDB 事务快照内可见、事务外不可见、回滚可整体撤销，故 §22 单日原子性不变。

  备选 `duckdb_sqlalchemy.copy_from_rows`（同为批量快路径，实测约 0.54 s，比注册视图慢约 3.8×）未采用，原因是它经 CSV 文本中转而非按 Python 对象直传：空字符串会被读成 NULL，首行含换行会让 DuckDB 的 CSV 试探直接抛 `InvalidInputException`。需要说明的是，这两个失真在当前契约下**均不可达**——事实表的文本列只有 `instrument_id` / `ts_code`（Provider 侧经 `_required_cell` + `strip()` 保证非空且形态固定）与常量 `source`。因此选择注册视图的理由是**性能（约 3.8×）与"类型按对象直传、不经文本序列化"的构造性无损**，而非已发生的缺陷；若未来新增可空或自由文本列，CSV 中转会成为真实风险。
- [trading_calendar 加列影响旧路径] → 全部 nullable，旧写入不填即可；测试覆盖旧路径回归。
- [与现有 Tushare Provider 共享 transport 的重构风险] → 渐进接入、不改业务契约、现有测试全量回归。
- [回填期间写锁竞争] → 网络等待在锁外，单日事务短；现有 quote/watchlist 写路径回归测试。
- [管理员页面误读大表] → 全部统计读 state 小表，API 层禁止 COUNT(*)。
- [错误信息泄漏 Token] → 错误文本过滤敏感配置，测试覆盖。

## Migration Plan

按技术方案 §75 Phase 0~8 分阶段实施（详见 tasks.md），每阶段 `pytest` 通过再进入下一阶段：

1. Phase 0 preflight：`git status`、`alembic heads`（当前 `0002_multi_user_auth`）、`pytest` 基线。
2. Phase 1 数据库 schema：基于实际 head 创建 `0003_a_share_historical_data`（纯新增表 + trading_calendar 可空加列，简单 ALTER 即可）+ 迁移测试。
3. Phase 2~4：Provider 扩展 → Repository/Validator → HistorySyncService（测试直接调 `service.run()` 验证连续性场景）。
4. Phase 5~7：Job 接入 lifespan → Admin API → Admin 页面。
5. Phase 8：全量回归（`pytest`、`pytest -m online`）+ 文档（README/CHANGELOG/config.example.yaml/版本号 v0.3.0）。

部署与回滚：真实数据升级前停服备份 `data/marketmind.duckdb`（Docker 按挂载路径）；容器既有"先 `alembic upgrade head` 再启动"机制自动完成迁移；0003 不改不删既有表，回滚 = 还原备份文件 + 回退代码版本。首次回填在 `/admin/data` 触发或等待 20:30 定时，失败停在水位、下次续跑。

## Open Questions

- ~~namechange 规范键是否需要扩展为 ts_code+name+start_date+end_date+ann_date：待在线 smoke test 验证同一 ts_code/name/start_date 是否存在多事件~~ **已结论（2026-09-19 在线验证）**：`000001.SZ` 全部 4 条事件，无重复键，`end_date`/`ann_date` 4/4 非空，三字段键 `ts_code+name+start_date` 成立，不扩展。
- ~~daily/daily_basic/moneyflow 的 6000 行 fallback 是否可批量多代码：待 smoke test 确认接口参数能力后决定分组粒度~~ **已结论（2026-09-19 在线验证）**：`daily`/`moneyflow`/`adj_factor` 支持逗号分隔多 `ts_code`（1000 代码全部返回）；**`daily_basic` 会静默返回 0 行**（不报错）。因此 `daily_basic` 的 fallback 按"候选集 − 已返回代码"逐只补齐，不复用其他接口的 multi-code fallback。
- ~~adj_factor 是否存在隐性返回上限~~ **已结论（2026-09-19 在线验证）**：当日 `adj_factor` 返回 5565 行、`daily` 5553 行，均未达 6000，无隐性更小上限；但只有 435~447 行余量（约 8%），截断 fallback 会越来越频繁地触发，需按 §33.2 保证补齐路径可用。注意两者覆盖不同（12 只停牌证券有复权因子但当日无行情）。
- 历史 metrics key 采用按 endpoint 拆分（tushare_history_daily 等）还是统一 tushare_history + endpoint 日志字段：实施 Phase 2 时按现有 ProviderMetricsRegistry 聚合粒度选择，不为此重构 observability。
- 每日 20:30 统一调度与 moneyflow 20:30 cutoff 同刻：默认可接受（首日 moneyflow 可能 WAITING_SOURCE 次日补齐）；如实测频繁等待，将调度调整为 21:00（仅配置变更）。
