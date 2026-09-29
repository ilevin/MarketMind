## Context

v0.3.1 历史同步的现行架构（经全量代码审计确认）：

- **水位模型**：`history_sync_state` 每数据集一行，`latest_complete_trade_date` 是唯一的连续水位，全市场粒度；`history_day_status` 以 `(dataset, trade_date)` 记整日 COMPLETE 账本（`app/models/history_sync.py:97/136`）。
- **推进路径**：`HistorySyncService._sync_day_level_dataset` → 逐交易日 `_sync_single_day` → 全市场单日一批（`_fetch_day`）→ 整批校验（`app/services/history/validation.py` 的 `validate_batch`）→ `_commit_single_day` 单事务（count → 整日 DELETE → staging 批量 INSERT → day_status → `complete_day` 推水位 → run_dataset 计数，全部在 `write_coordinator.write()` 内）。**批内任一证券异常即整日 FAILED、水位停在失败日前**（`sync_service.py:844-851`）——这是本次改造要消除的结构性阻塞。
- **重试**：`RetryPolicy`（纯退避计算：`min(5×2^(attempt-1), 300)` 秒 × [0.8,1.2] 抖动）+ `CONFIG_ERROR_CODES`（Token 缺失/权限/SCHEMA_MISMATCH/UNKNOWN_INSTRUMENT/ALIAS_CONFLICT 首试即终态）；`max_attempts=10` 语义为**总尝试次数**（`app/config.py:94`）。
- **Provider**：`TushareHistoricalMarketDataProvider` 只有按 `trade_date` 全市场（主路径）与"单只×单日"（截断 fallback）两种请求形态；SDK 支持的 `pro.daily(ts_code=…, start_date=…, end_date=…)` 区间形态项目未封装。别名层 `normalize_historical_aliases` 以 `ts_code` 为分组键做新旧码并存检测——**该分组假设"单日批次内每证券至多一行"，区间形态下天然失效**。
- **写入**：`HistoryFactRepository` staging 视图 + `INSERT … SELECT` 批量写（chunk 1000），整日 DELETE+INSERT 替换幂等；基准 `scripts/bench/bench_history_write.py` 守护非逐行退化。
- **执行记录**：`history_sync_run`（run 主表）+ `history_sync_run_dataset`（(run_id, dataset) 详情）；`requested_by_user_id` 为 String(64)。
- **约束**：DuckDB 1.5.5 单写者（uvicorn --workers 1）、全库不用 UNIQUE 约束与二级索引（唯一性由应用层写锁内保证）、`TushareRequestGate` 进程级 endpoint 限流（默认 0.6s，stock_basic ≥1.25s）、`AvailabilityPolicy` 按北京时间 cutoff 计算各数据集目标日、严格交易日历禁降级。

产品与技术方案见《../temp/MarketMind_v0.3.1_个股级历史数据同步改造方案.md》（v0.4.0）：水位下沉到"数据集 × 股票"、失败隔离、自动补偿；同时明确 V1 不做并发、不做通知、不建 `sync_retry` 表。

## Goals / Non-Goals

**Goals:**

- 每个 `(dataset, instrument)` 拥有独立水位（`stock_sync_state.watermark_date`），单股失败只影响自己，不阻塞同数据集其他股票、不使 Run 失败。
- 单股任务"首次执行 + 最多 3 次重试"全部记录在同一条 `sync_task`；失败股票无需人工补偿，下一轮按落后水位自动补齐。
- 单股"事实区间替换 + 水位推进 + task 成功"在同一 WriteCoordinator 写事务内原子提交；网络请求全部在锁外；幂等不产生重复事实行。
- 股票生命周期边界正确（上市前/退市后不产生伪缺口；停牌等合法空结果不判失败）。
- 管理端可见：数据集个股完整度与今日成功/失败统计；`/admin/data/stocks` 个股列表（筛选/搜索/100 条分页）与失败任务详情。
- v0.3.1 数据库可安全升级（0004 迁移只增不删），既有事实数据不丢失。
- 全部新行为有自动化测试；默认 pytest 离线。

**Non-Goals:**

- 失败通知（邮件/短信/Webhook）、`sync_retry` 队列表、人工修改水位/"强制跳过"。
- 多进程/多 Worker 并发同步、Redis/Celery、并发网络 fetch + 串行 writer。
- 事实表业务字段与四张旧同步控制表结构的破坏式变更（旧表保留，旧字段冻结）。
- `sync_task` 保留/清理策略（增长可接受，另行演进）。
- `history_day_status` 账本在新模式下继续写入（见 D8：该账本属日级模型，个股模式停写，历史行保留）。

## Decisions

### D1. 状态键用 `instrument_id` 而非 `ts_code`（与方案文档的有意偏差）

`stock_sync_state` 的逻辑唯一键为 `(dataset, instrument_id)`，另冗余 `ts_code` 列（成功同步时刷新为当前主档代码，仅供展示/日志）。`sync_task` 同时保存 `instrument_id` 与 `ts_code`（请求当时使用的规范代码）。

理由：项目的证券身份主键是 `instrument_id`（`CN:STOCK:<symbol>`），v0.3.1 别名层的存在意义正是"ts_code 会因代码变更而改变、身份不变"。若状态键用 ts_code，下一次证券代码变更后该股状态行将与主档断链（旧码行成孤儿、新码行从零开始）。用 instrument_id 则任何代码变更下水位连续。方案文档的 `ts_code` 是 Tushare 视角的简化记法；实现按项目身份模型校正。页面/API 对外仍展示 ts_code（JOIN `cn_stock_basic`）。

### D2. 两张新表的结构与项目惯例对齐

`stock_sync_state`（状态表，每 `(dataset, instrument_id)` 一行）与 `sync_task`（任务流水，每次启动同步一行）均按项目 DuckDB 惯例建模（`app/models/history_sync.py` 风格的 ORM）：

- **无 UNIQUE 约束、无二级索引、无 FK**——与既有同步控制表与事实表的建模惯例一致（主档表 cn_stock_basic/cn_stock_company 例外地持有对 instrument 的 FK，同步控制表与事实表从不使用；全库零 `create_index`）。`stock_sync_state` 唯一性由写锁内 get-or-create 保证并加测试；表规模（4 数据集 × ~6000 股 ≈ 2.4 万行）与 `sync_task` 查询模式（按 id 主键直查、列表不查此表）在 DuckDB 列存下无需索引。方案文档 §33 的索引建议**不采纳**，理由记录于此。
- `stock_sync_state` 列：`dataset`、`instrument_id`、`ts_code`（冗余）、`watermark_date`（可空）、`last_task_id`（可空）、`last_status`（success/failed，可空）、`last_error_code`、`last_error`（Text）、`last_success_at`、`last_attempt_at`、`created_at`、`updated_at`。水位单调不下降由 `advance_watermark()` 在写事务内校验（new < old 抛错回滚）。
- `sync_task` 列：`id`（BIGINT，由显式 sequence `seq_sync_task_id` 生成——沿用 `seq_tag_id`/`seq_user_id` 既有模式）、`run_id`（String(64)，与 `history_sync_run.run_id` 类型一致、逻辑关联不建 FK）、`dataset`、`instrument_id`、`ts_code`、`start_date`、`end_date`、`status`（running/success/failed/interrupted）、`retry_count`（0~max_retries）、`attempt_count`（1~max_retries+1）、`records_fetched`、`records_written`、`error_code`、`error_type`、`error_message`（Text，经 `_safe_error_text` 脱敏截断，无 traceback）、`started_at`、`finished_at`、`duration_ms`、`created_at`。
- `history_sync_run_dataset` 增列（简单 `op.add_column`，server_default 0）：`processed_count`、`task_success_count`、`task_failed_count`、`skipped_count`——Run 语义调整后的数据集级统计落点。

### D3. 初始个股水位：统一 NULL 全量回填（宁可重复拉取）

不采用"从事实数据推导连续边界"的复杂算法，0004 迁移**不写入任何 `stock_sync_state` 行**（首轮 run 时按 universe 批量补建）：

- 严格连续检查（"无行即缺口"）在 A 股现实下必然退化——停牌日 daily 天然无行，几乎每只股票都有停牌史，多数股票会被回退到上市初期，推导计算白做而结果趋近全量重拉；
- 上界 = 旧 dataset 水位只能证明"旧机制认为整日完成"，无法证明个股无缺口，任何信任它的推导都在认证缺口（方案文档 §21 自己的警告）；
- NULL 全量重拉成本可控：每股每数据集**一次区间请求**（非逐日），约 6000 股 × 4 数据集 = 2.4 万次请求，0.6s gate 节流下约 4 小时，可跨多轮自动完成；写入为区间替换，幂等无重复；
- 迁移因此保持"纯 DDL + 少量加列"，可重复执行、校验简单、回滚安全。

迁移附带**只读诊断统计**（不推进任何水位）：各数据集事实行数、有行股票数、MAX(trade_date) 分布，写入迁移日志帮助运维预估回填规模。旧 `history_sync_state.latest_complete_trade_date` 冻结在迁移前值（不再推进、不删除），`/runs` 历史展示兼容。

### D4. Provider 新增按股票区间接口

`HistoricalMarketDataProvider` Protocol 与 `TushareHistoricalMarketDataProvider` 新增：

```python
def get_history_by_stock(
    self, dataset: DatasetName, instrument: Instrument,
    start_date: date, end_date: date,
) -> ProviderBatch[DailyBar | AdjFactor | DailyBasic | MoneyFlow]
```

- 映射 `pro.{daily,adj_factor,daily_basic,moneyflow}(ts_code=…, start_date=…, end_date=…, fields=…)`；`ts_code` 由 `_ts_code_of_instrument(instrument)` 构造（主档当前代码），**不按代码首位推断**。
- 传入 `Instrument` 而非裸 ts_code（方案文档签名的有意校正）：Provider 无 DB 访问（现状纪律），instrument 由 Service 在 run 开始时读主档快照传入；返回行经别名层改写后仍用 `_instrument_for_ts_code` 映射，与现有两条路径同构。
- `raw_row_count` 仍取别名规范化前的上游行数；单股 16 年日线约 4000 行 < 6000 上限，但保留 `raw_rows >= DAILY_ROW_CAP → truncation_risk=True` 防护（异常放大返回时拒绝提交）。
- Registry `_METHOD_DATASETS` 登记该方法（metrics 键复用 `tushare_history_{dataset}`），`call_with_metrics` 包装、共享 `TushareRequestGate`、SDK 单请求超时，全部沿用。
- **不删除**现有按 trade_date 的主路径方法（Protocol 兼容、fallback 语义保留），但 Service 的日级编排不再调用它们（D7）。

### D5. 别名层扩展到区间形态（关键正确性点）

`normalize_historical_aliases` 的分组键从 `canonical_ts_code` 改为 `(canonical_ts_code, trade_date)`：

- 现有实现的"同一规范代码多行 → 新旧并存检测"隐含单日批次假设；区间批次中同一证券每天各一行，不改会把正常多日行误判为别名冲突。
- 分组键加日期后对单日输入行为等价（单日内 (code, date) 分组与 code 分组相同），现有 55 个单测语义保持；新增区间场景单测（同股多日正常、区间内某日新旧并存一致/冲突）。
- 其余语义不变：仅旧码改写、一致去重（`action=drop_legacy` WARNING）、冲突抛 `ALIAS_CONFLICT`（配置类错误，该股首试终态失败，不阻塞其他股票）、`raw_row_count` 改写前取值、主档数据集不接入。

### D6. 校验层支持区间批次

校验层（`validation.py` 的 `validate_batch`，模块级函数而非类）增加区间模式：传入 `date_range=(start, end)` 时"记录日期与请求一致"放宽为"日期 ∈ [start, end] 且不早于该股 list_date、不晚于 min(end, delist_date)"；批内 `(instrument_id, trade_date)` 唯一、NaN/Inf、各数据集专项规则全部沿用。单日模式保留（`get_daily` 等旧接口与其测试不动）。

### D7. 同步编排：单股执行器 + 股票串行循环

`HistorySyncService` 重构为两层（方案文档 §31 分层的落地）：

- **`StockSyncExecutor`**（新，`app/services/history/stock_executor.py`）：单股单数据集一次同步的完整生命周期——创建 `sync_task`(running) → 循环最多 4 次 attempt（锁外 fetch → 锁外 validate → 写锁内原子提交/失败记录）→ 重试间隔复用 `RetryPolicy.sleep_before_retry`（sleep 前后检查 cancellation）。终态：success（水位推进）或 failed（水位不动）。每股每 attempt 的异常分类沿用 `_error_code_of` + `is_config_error`（配置类首试即终态）。
- **`HistorySyncService`**（编排，保留入口 `run(trigger, …)` 不变）：run 创建 → `recover_stale_runs`（扩展，见 D9）→ 主档前置（不变）→ 对四个日级数据集顺序执行：解析 target（`AvailabilityPolicy` 不变）→ 批量补建缺失 `stock_sync_state` 行（一次写事务，避免首轮 2.4 万次逐条 ensure）→ 取 universe（`list_cn_stock_instruments`，含退市）按 `watermark_date ASC NULLS FIRST, ts_code ASC` 排序 → 逐股：计算有效区间（D8）→ 无工作则 skipped_count+1、不建 task、不发请求 → 有工作交 `StockSyncExecutor` → **单股任何 `StockSyncError` 记录后 continue**；每股票边界检查 cancellation（收到信号：完成当前股、持久化、run 标 INTERRUPTED）。系统级异常（数据库不可用、日历失败、框架崩溃）照旧上抛终止 Run。
- 日级数据集的旧单日路径（`_sync_day_level_dataset`/`_sync_single_day`/`_fetch_day`/`_daily_basic_fallback` 及 `history_day_status` 写入）**移除**；日级 reconcile（`reconcile_daily_watermarks`）随之退役——个股水位的正确性由单股原子事务保证，缺口自愈靠落后发现，无需跨表对账。
- 退市股处理：`delist_date` 非空且 < `history.start_date` 的股票不进 universe 计算有效区间（无工作）；退市股在退市前区间内仍正常同步（历史数据有价值）。

### D8. 有效区间与空结果语义

每股有效同步范围：`eff_start = max(history.start_date, list_date)`（list_date 缺失保守取 history.start_date）、`eff_end = min(target, delist_date)`（delist_date 缺失取 target）。`watermark < eff_end` 才有工作，请求区间 `[watermark+1 的次个严格交易日 … eff_end]`（严格日历约束，不用 date+1）。

**空结果判定**（方案文档 §8.3 的落地，逐一确认四个数据集语义）：

- 请求**无异常**返回 0 行 = 上游对区间给出有效响应 → **允许推进水位**（长期停牌、moneyflow 非覆盖证券的合法形态；与现行"证券级自然缺失不判日期级不完整"语义一致）。`records_fetched=0` 落 task，日志 WARNING。
- 请求抛错（超时/限流/Schema）→ 正常进入重试/失败路径。
- "当日数据未发布"场景由调度端吸收：target 恒为已过 cutoff 的交易日（`AvailabilityPolicy`），个股请求的 `end_date` 不会指向未发布日，现行 `WAITING_SOURCE`/`EMPTY_RESULT` 状态与错误码在个股路径不再产生（错误码定义保留，历史数据兼容）。
- 每数据集空结果语义以 `@pytest.mark.online` smoke 实测确认（停牌股区间、退市股末段、moneyflow 非覆盖股），结论固化进离线测试的 fake 响应。

### D9. 中断恢复扩展到 task

`recover_stale_runs` 在现有逻辑（stale run → INTERRUPTED；state 三态回落）之外增加：把属于已中断 Run 的 `running` 状态 `sync_task` 批量置 `interrupted`（`finished_at=恢复时间`）。**绝不推进对应 `stock_sync_state.watermark_date`**（task 未提交，水位本就未动）；下一轮按原水位重新创建新 task。恢复放在每次 `run()` 开头的现有写锁事务内，一次批量 UPDATE。

### D10. Run 语义与进度暴露

- `RunStatus`：新逻辑只产生 RUNNING / SUCCESS（正常跑完，允许有个股 failed）/ FAILED（系统级：主档硬前置失败、数据库异常、框架崩溃）/ INTERRUPTED（停机信号或进程退出）/ NOOP（全部数据集无工作）。`PARTIAL` 枚举保留（历史行兼容）但不再产生——"部分个股失败"改由 `run_dataset.task_failed_count` 表达。
- `history_sync_run_dataset` 语义调整：`start_watermark`/`target_trade_date`/`end_watermark`/`dates_completed`/`failed_trade_date` 冻结为兼容列（新写入置 NULL/0），新列 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count` 承载统计；`rows_written`/`request_count`/`retry_count` 继续累计（口径=个股区间写入行数/请求次数/重试次数）。
- 实时进度（当前 dataset/ts_code/已处理/成功/失败/跳过）：`HistorySyncService` 持有进程内 progress 快照对象，API 经 `request.app.state` 读取——高频变化的"当前 ts_code"不逐股写库；重启后 run 即 INTERRUPTED，内存进度丢失可接受。聚合计数以 DB 为准（progress 仅展示）。

### D11. 今日成功/失败统计口径

- **今日成功**：`stock_sync_state.last_attempt_at`（转 Asia/Shanghai 日期）= 今天 **且** `last_status='success'`；今日失败同理。即"该股该数据集今天最后一次完成任务的状态"，与方案文档 §24 一致（09:00 failed ×2 + 09:10 success → 成功 1 失败 0）。
- SQL 侧 `last_attempt_at AT TIME ZONE 'Asia/Shanghai'` 取日期比较，与服务器部署时区无关。
- summary 数据集卡片 = 对 `stock_sync_state`（2.4 万行小表）的一次 GROUP BY 聚合 + universe 计数，DuckDB 毫秒级，无需缓存、不扫事实表。

### D12. API 与前端

- `GET /summary`：URL 不变，`daily_datasets[]` 字段升级为个股口径（新增 `stock_count`/`up_to_date_count`/`lagging_count`/`today_success_count`/`today_failed_count`/`completion_rate`；`record_count`/`data_min_date`/`data_max_date`/`last_success_at` 保留自 `history_sync_state`；旧水位字段保留输出冻结值）。`overall_status` 增设语义：有 active run → RUNNING；系统级 FAILED → ERROR；无 FAILED 但存在 lagging 股票 → LAGGING（"有缺口"，**单股失败不升级 ERROR**）；全部 up_to_date → HEALTHY。
- `GET /stocks?dataset=&status=all|success|failed&q=&page=`：`page_size` 服务端固定 100（超出钳制）；查询 = `cn_stock_basic LEFT JOIN stock_sync_state ON instrument_id AND dataset=?`，筛选/搜索（name/ts_code LIKE，服务端）在 SQL 完成；默认排序 `last_status='failed' DESC → watermark_date ASC NULLS FIRST → ts_code ASC`；响应含 summary 统计块 + 分页元信息。不加载全量股票到浏览器。
- `GET /tasks/{task_id}`：按 id 直查 `sync_task` + JOIN 主档补 stock_name；404 处理不存在。
- `POST /sync`：语义、202/409/CSRF、服务端取 `requested_by_user_id` 全部不变（顺手修复 `CurrentUser.user_id`(int) 直接传入 String 列的既有类型瑕疵）。
- 前端：`/admin/data` 卡片改个股口径；新增 `/admin/data/stocks` 模板（复制现有 topbar 模式 + "个股历史"导航；数据集切换复用 `.chip`；列表复用 `.table`/`.status-badge`；失败详情复用 `.modal-overlay`/`.modal` 模式，错误信息放可滚动区域且经 `esc()` 转义；分页为全站首个分页控件，简单"上一页/下一页/共 N 条 第 x/y 页"）。JS 逻辑加在 `app/static/app.js`（`body[data-page]` 分发现有模式），不引入框架/构建链。

### D13. 配置语义

`HistoryConfig` 新增 `max_retries: int = 3`（个股重试次数，总尝试 = 4）；`max_attempts` 字段保留兼容读取：用户显式配置了 `max_attempts` 且未配置 `max_retries` 时，`max_retries = max_attempts - 1` 并 WARNING 提示语义变化；两者都配置以 `max_retries` 为准。`backoff_initial_seconds`/`backoff_max_seconds`/`jitter_ratio` 不变（个股重试序列约 5/10/20 秒）。`RetryPolicy` 增加 `max_retries` 属性（由 config 换算），`_handle_attempt_failure` 语义同步。其余配置项（start_date/schedule_time/startup_catchup/限流/availability/主档刷新）不变。

### D14. 写入路径与性能守护

- `HistoryFactRepository` 新增 `delete_for_instrument_range(dataset, instrument_id, start, end)` 与区间替换提交方法（复用 `_insert_rows_via_staging` 批量写）；单股 16 年约 4000 行一次事务，粒度小于现行整日 6000 行，天然满足批量基准。
- `scripts/bench/bench_history_write.py` 增加个股区间替换场景（单股 4000 行 × N 股连续提交），断言语句数与提交次数不退化。
- 每股固定 2~3 个短写事务（task 创建、成功提交或失败记录），无网络持锁；run 开始的批量补建状态行是额外 1 个事务。

## Risks / Trade-offs

- [全量回填期间数据"看似回退"] → 迁移后首轮 run 前所有股票 watermark=NULL，数据总览完成率从 0% 逐股上升，旧事实数据全程保留（仅成功时区间替换）。CHANGELOG 与页面文案明示"升级后需一次全量回填（约数小时，跨多轮自动完成）"。
- [上游区间接口真实行为未知（跨年区间返回顺序、字段完整性、单次行数上限、停牌日是否有 adj_factor）] → 实现前先跑只读 online spike（`scripts/spike/`）实测四数据集 ×（正常股/停牌股/退市股末段/旧代码股）的区间响应，结论固化进离线 fake；未确认前不合并调度改造。
- [别名层分组键改造引入回归] → 分组键变化对单日输入等价（证明见 D5），55 个既有单测 + 新增区间场景单测 + 集成测试（旧代码股区间回填落规范码）三重守护。
- [`sync_task` 无界增长（约 600 万行/年量级）] → V1 接受（DuckDB 列存 + 主键直查无压力，列表页不查此表）；保留策略列为后续演进，不阻塞本期。
- [股票串行使单轮全量回填耗时约 4 小时（gate 0.6s × 2.4 万请求）] → 有意取舍（方案文档 §26：V1 不并发）；跨轮自动续跑；已最新股票零请求。
- [今日统计依赖 `last_attempt_at` 的时区换算] → 统一 `AT TIME ZONE 'Asia/Shanghai'` + 多时区部署单测（现有 `BUSINESS_TZ_NAME` 常量复用）。
- [summary 响应结构升级破坏旧前端] → 前端同版本同步改造；`/runs`、`/runs/{run_id}` 字段尽量保持兼容；无外部 API 消费者（管理端自用）。
- [旧 `WAITING_SOURCE`/日级 reconcile 退役导致行为差异] → 语义由 target 计算与个股空结果规则吸收（D8）；相关既有测试改写而非删除，覆盖点平移到新路径。

## Migration Plan

1. 实施顺序（与 tasks.md 对应）：迁移与模型 → Provider 区间接口 + 别名层 → Executor 与编排 → API → 前端 → 测试回归 → online smoke → 发布。
2. 部署：停服务 → 成对备份 `marketmind.duckdb` 与存在的 `.wal` → 启动新镜像（容器启动先 `alembic upgrade head` 执行 0004：建两表 + `seq_sync_task_id` + run_dataset 加列 + 纯增量校验，失败容器退出保持迁移前状态）→ 验证 `/health`、`/admin/data` → 手动触发首轮同步小规模观察 → 放开定时。
3. 首轮同步 = 全量个股回填（D3），跨多轮自动完成；期间页面正常展示逐股进度。
4. 回滚：停新版本 → 恢复升级前 `.duckdb` + `.wal` → 启动 v0.3.1 镜像。**不支持** v0.3.1 直接运行在 0004 迁移后的数据库上（多出的表不破坏 v0.3.1 读写，但同步状态两套模型不一致，须走备份恢复）；0004 downgrade 仅提供 DDL 逆操作（删两表/列），生产回滚以文件备份为准（与 0002/0003 同约定）。

## Open Questions

1. **四数据集区间空结果语义的实测确认**（停牌股/退市股末段/moneyflow 非覆盖股的 0 行区间响应是否无异常返回）——online spike 先行，结论决定 D8 fake 测试基线。（阻塞 tasks 的 spike 项，不阻塞离线开发）
2. **单股超长区间的行数上限行为**（16 年 ≈ 4000 行远低于 6000，但需实测确认无隐藏分页）——同上 spike 覆盖。
3. （已决）`stock_sync_state.ts_code` 冗余列刷新时机：成功同步时刷新为当前主档规范代码（specs "个股独立连续水位线" 已定稿）；run 开始批量补建状态行时写入主档当前代码。
4. `sync_task.error_type` 是否有必要独立于 `error_code`（现方案保留两列，实现时若发现冗余可合并——迁移前定稿）。
