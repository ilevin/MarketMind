## Why

v0.3.1 的历史同步以"数据集 × 交易日"为推进单位：每个交易日把全市场约 6000 只证券作为**一批**拉取、整批校验、整日原子提交（`_sync_single_day`，`app/services/history/sync_service.py:919`）。批内任一证券异常（UNKNOWN_INSTRUMENT、DUPLICATE_KEY、字段非法）即整日 FAILED，该数据集本轮停止推进、水位停在失败日前（`sync_service.py:844-851`）。随着回填时间跨度增大，每一次证券代码变更、每只长期异常股、每条上游脏数据都会放大为**整个数据集的永久阻塞点**——v0.3.0 首次回填即因单个历史 ts_code 别名卡死在 2010-01-04，v0.3.1 的别名登记表只是个案补丁。产品与技术方案（《../temp/MarketMind_v0.3.1_个股级历史数据同步改造方案.md》，v0.4.0）给出结构性解法：**水位下沉到"数据集 × 股票"，单只股票失败只影响自己，失败股票由下一轮落后水位检测自动补偿**。本变更将该方案落为实现。

## What Changes

- **水位下沉**：新增状态表 `stock_sync_state`，每个 `(dataset, instrument_id)` 一行，维护自己的 `watermark_date`（最后确认完成的有效交易日边界）；`ts_code` 为冗余展示列（成功同步时刷新为当前主档代码），证券身份以 `instrument_id` 为准——代码变更下水位连续。失败绝不推进水位；补偿依据 = `watermark_date < 当前目标水位` 的落后发现，**不建 `sync_retry` 队列表**。
- **新增任务流水表 `sync_task`**：每次 `dataset × 股票` 实际启动一次同步就新增一条（不覆盖历史）；记录 `run_id`（关联现有 `history_sync_run`）、instrument_id 与 ts_code、同步区间、`status`（running/success/failed/interrupted）、`retry_count`（0~max_retries）、`attempt_count`（1~max_retries+1，默认 4）、records_fetched/written、结构化 error_code/type/message。Run = 一次全局调度，Task = Run 中的一只股票 × 一个数据集，两层概念不再混为一层。
- **Provider 新增按股票区间拉取**：`TushareHistoricalMarketDataProvider` 增加 `get_history_by_stock(dataset, instrument, start_date, end_date)`（传入 Instrument 快照，请求 ts_code 由主档当前代码构造；映射 `pro.daily(ts_code=…, start_date=…, end_date=…)` 等四个日级 endpoint，SDK 已支持该参数形态但项目未封装）。显式 `fields` 声明、共享 `TushareRequestGate` 限流、raw_row_count 口径沿用；v0.3.1 alias 规范化沿用并扩展——并存检测分组键改为 `(canonical_ts_code, trade_date)` 以支持区间批次（对单日批次行为等价），其余语义不变。
- **同步粒度重构**：`HistorySyncService` 的日级推进逻辑从"按交易日全市场一批"改为"每数据集 → 股票串行（`watermark_date ASC NULLS FIRST, ts_code ASC`，落后优先）→ 单股一次请求覆盖完整缺失区间（start=watermark+1，end=target，不逐日请求）"。单股任务内首次执行 + 最多重试 `max_retries` 次（默认 3；复用现有 `RetryPolicy` 退避与错误分类，配置语义改清晰）；单股失败记录后 `continue` 下一只；系统级异常（数据库不可用、日历前置失败）仍终止 Run。
- **股票生命周期边界**：有效同步起点 `max(history.start_date, stock.list_date)`，有效终点已退市取 `min(target, delist_date)`；上市前/退市后不产生"缺失"或"失败"；停牌等合法空结果结合证券生命周期、数据集特性与 Tushare 响应语义判断，不统一视为错误（逐一确认四个数据集的空结果语义并测试）。
- **单股原子事务**：单股一次同步成功时，"事实数据区间替换 + `stock_sync_state` 水位推进 + `sync_task` 成功状态"在同一个 `WriteCoordinator` 写事务内提交；网络请求、normalize、校验全部在事务与写锁外。幂等沿用区间 DELETE + 批量 INSERT（`HistoryFactRepository` staging 批量写），事实表结构与业务字段不变。
- **Run 语义调整**：Run 的 `success` 表示正常执行完毕（允许存在少量个股 task failed）；`failed` 仅表示系统级错误（数据库不可用、日历前置失败、任务框架崩溃）；`interrupted` 表示进程退出未完成。个股失败数量经统计字段/API 单独表达，不再把 5935 只中 1 只失败渲染为整体异常。
- **API 增量扩展**：`GET /api/admin/history-data/summary` 保留 URL、响应模型升级为个股口径（stock_count / up_to_date_count / lagging_count / today_success_count / today_failed_count / completion_rate）；新增 `GET /api/admin/history-data/stocks`（dataset、status=all|success|failed、名称/代码搜索、服务端分页固定 page_size=100）与 `GET /api/admin/history-data/tasks/{task_id}`（失败详情）。`POST /sync` 语义不变，不加 force/skip/reset 参数。
- **UI**：`/admin/data` 顶部与四个数据集卡片改为个股口径（今日成功/失败 = 今天该股最后一次完成任务的状态，按 Asia/Shanghai 业务时区统计；完整度 = 已到目标水位股票占比）；新增 `/admin/data/stocks` 个股历史页面（数据集切换、统计区、成功/失败筛选、名称/代码搜索、100 条分页列表、默认排序"失败优先→水位较旧→ts_code"、失败可点击打开只读详情 modal）。复用现有 admin layout、badge、modal、表格样式与 `api()`/`esc()` 工具，不引入前端框架。
- **迁移（基于当前 head `0003_a_share_historical_data` 创建 `0004_*`）**：创建 `stock_sync_state` 与 `sync_task`、为 `history_sync_run_dataset` 增加个股统计列；**初始个股水位统一为 NULL、不做推导**——旧整日水位无法证明个股无缺口，任何信任它的推导都在认证缺口，故宁可首轮全量区间回填（区间替换幂等，约数小时、跨多轮自动完成；宁可重复拉取，不可错误跳过缺口）；迁移附带只读诊断统计（各数据集事实行数、有数据股票数、最大交易日分布）帮助预估回填规模；不删除旧同步控制表、不修改既有数据；迁移可重复执行并内置校验。
- **进程中断恢复扩展**：启动恢复把遗留 `running` 的 `sync_task` 标记 `interrupted`，对应 `stock_sync_state.watermark_date` 绝不推进，下一轮按原水位自动重新同步。
- **配置**：`history` 新增 `max_retries=3`（单股单任务总尝试 = max_retries + 1，即默认 4 次）；`max_attempts` 为兼容保留字段（默认 10）——用户显式配置且未配置 `max_retries` 时换算 `max_retries = max_attempts - 1` 并 WARNING，两者同时配置以 `max_retries` 为准；其余（start_date、schedule_time、startup_catchup、限流间隔、availability cutoff）保持。
- **保留不变**：`TushareRequestGate` 全局限流（不因按股票请求绕过）、availability cutoff、严格交易日历、主档前置与刷新周期、v0.3.1 alias resolution（`ALIAS_CONFLICT` 只使该股任务失败、不阻塞数据集）、`WriteCoordinator` 与 uvicorn 单 worker 单写者、三种触发入口（定时/启动补齐/手动）共用同一 orchestrator。
- **明确不做**（非目标）：失败通知（邮件/短信/Webhook）、`sync_retry` 表、多进程/多 Worker 并发、Redis/Celery 外部队列、管理员手工修改水位或"强制跳过"、事实表业务字段改造、每次重试单独一条 `sync_task`。无 BREAKING 数据库变更（只增不删）。

## Capabilities

### New Capabilities

（无——个股级同步属既有 `historical-data-sync` 能力的模型演进，个股管理页属既有 `admin-data-management` 能力，不新建能力。）

### Modified Capabilities

- `historical-data-sync`: 核心重写——水位模型从数据集级 `latest_complete_trade_date` 下沉为个股级 `stock_sync_state.watermark_date`；同步粒度改为 `dataset × ts_code`；单股"首次+最多 3 次重试"替换单日 10 次尝试；单股失败不阻塞其他股票、不终止 Run；自动补偿基于落后水位；股票生命周期边界与空结果语义；执行记录新增 `sync_task` 流水（Run/Task 两层）；进程中断恢复扩展到 task；今日成功/失败统计口径。
- `history-provider`: 新增按股票区间拉取要求（`get_history_by_stock`：instrument + start/end、显式 fields、共享 request gate、行数上限防护）；alias 规范化沿用并扩展（并存检测分组键加 trade_date 以支持区间批次，单日行为等价），除此之外既有要求保持。
- `admin-data-management`: `/admin/data` 数据集卡片与 summary API 改为个股完整度 + 今日成功/失败口径；新增 `/admin/data/stocks` 个股历史页面与 `/stocks` 分页列表、`/tasks/{task_id}` 详情 API；overall_status 增设"有缺口"语义（个股失败不升级为整体异常）；失败详情只读 modal。
- `historical-data-storage`: 事实表统计维护改为随单股区间替换增量维护（表结构与业务字段不变）。
- `db-migration`: 版本链新增 `0004_*`（两张新表 + run_dataset 统计列 + 初始水位统一 NULL + 只读诊断统计 + 校验），不破坏既有表与数据，不删除旧同步控制表。
- `config-management`: `history` 重试配置语义改清晰（max_retries / max_attempts 转换），今日统计统一 Asia/Shanghai 业务时区。

## Impact

- **数据库**：新增 2 张表（`stock_sync_state` 约 4 数据集 × 6000 股、`sync_task` 随执行增长），0004 迁移为纯结构变更 + 只读诊断统计（不写状态行、不推导水位），可重复执行并内置校验；四张事实表、四张旧控制表结构不变；旧 dataset 水位字段进入兼容/汇总用途，本版本不退役。
- **Provider 层**：`app/providers/history/tushare.py` 新增按股票区间方法（10 个公开方法 → 11+），`app/providers/base.py` Protocol 同步扩展；别名层、gate、transport 不动。
- **服务层**：`app/services/history/sync_service.py` 核心重构（调度循环从逐日改为逐股、新增 StockSyncExecutor 职责边界）、`planner.py`（个股区间计算）、`retry.py`（语义与配置）；`app/models/` 与 `app/repositories/` 新增个股状态与任务两套模型/仓储，`history_fact.py` 新增单股区间替换写方法。
- **API/前端**：`app/api/admin_history.py`、`app/schemas/history_admin.py`（summary 响应升级 + 2 个新端点）；`app/templates/admin_data.html` 调整 + 新增 `admin_data_stocks.html`；`app/static/app.js` 新增个股页逻辑（分页/筛选/搜索/modal）。summary 响应结构变化由前端同步消化，`/runs` 与 `/runs/{run_id}` 尽量保持兼容。
- **配置**：`config.example.yaml` 更新 `history` 重试项；缺省按默认值运行。
- **测试**：`tests/integration/test_history_sync_service.py` 等既有守护测试随行为更新；新增个股水位/失败隔离/自动补偿/生命周期/今日统计/分页搜索等单测与集成测试（临时真实 DuckDB + fake Provider）；迁移测试覆盖全新库、正常 v0.3.1 库、含缺口库、含旧代码 alias 库、有 WAL 备份副本；默认 pytest 全离线，真实 Token 仅 `@pytest.mark.online` smoke；`scripts/bench/bench_history_write.py` 基准适配单股写入防退化。
- **运维**：升级遵循停服务 → 成对备份 `.duckdb` 与 `.wal` → 启动执行迁移 → 验证 `/health` 与 `/admin/data` → 小范围验证同步；回滚 = 恢复备份 + 启动 v0.3.1 镜像（不让 v0.3.1 直接运行在迁移后的库上）。
- **请求量模型**：从"按日全市场"变为"按股票区间"，首次 2010→今回填的总请求数与耗时明显变化（每股票每数据集至少 1 次请求，约 6000 股 × 4 数据集，受 0.6s gate 节流）；日常增量仅落后股票发请求、一次请求覆盖完整缺失区间；V1 不以并发作优化手段。
