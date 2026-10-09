## MODIFIED Requirements

### Requirement: 主档前置与刷新周期

统一同步 SHALL 先完成硬前置（trade_cal 缺失即刷新、stock_basic 超过 24 小时未成功则刷新），再刷新非阻塞主档（stock_company、namechange 默认每 7 天，namechange 增量带 7 天重叠窗口以吸收迟到修订，bootstrap 逐只推进 master_cursor 可中断续跑），最后依次执行日级数据集——股票四个日级数据集在前，`history.etf_enabled=true`（默认 true）时按"ETF universe 刷新为非硬前置"完成 etf_basic 周期刷新后再执行 etf_daily、etf_adj_factor；`etf_enabled=false` 时跳过 ETF 段。主档刷新失败时：trade_cal/stock_basic 失败 SHALL 阻止日级数据集推进；stock_company/namechange 失败 SHALL NOT 阻塞日级数据集；etf_basic 失败 SHALL NOT 阻塞股票日级数据集（仅令 ETF 两个数据集本轮失败，见"ETF universe 刷新为非硬前置"）。namechange 重叠窗口写入 SHALL 删除窗口内旧事件后插入完整返回集、保留更早历史。

#### Scenario: 硬前置失败阻止日级推进

- **WHEN** trade_cal 严格刷新失败
- **THEN** 四个股票日级数据集本轮不推进，run 记录失败原因（CALENDAR_UNAVAILABLE）

#### Scenario: namechange 增量重叠窗口

- **WHEN** namechange 增量刷新（start_date=上次成功-7天）
- **THEN** 窗口内旧事件被替换为当前完整返回，窗口外历史事件保留

#### Scenario: etf_basic 失败不阻塞股票数据集

- **WHEN** etf_basic 周期刷新失败而 trade_cal/stock_basic 正常
- **THEN** 四个股票日级数据集照常推进，仅 ETF 两个数据集本轮失败

### Requirement: 个股独立连续水位线

`daily`、`adj_factor`、`daily_basic`、`moneyflow`、`etf_daily`、`etf_adj_factor` 六个日级数据集 SHALL 在 `stock_sync_state` 表中以 `(dataset, instrument_id)` 为逻辑唯一键，为每只股票/ETF 独立维护 `watermark_date`（含义：该证券从有效同步起点起截至该日的应同步数据已连续、完整成功；NULL 表示尚未确认任何边界，从有效起点全量同步）。水位 SHALL 仅在单股区间原子提交成功的同一事务内推进，SHALL 单调不下降（提交事务内校验新水位早于旧水位即抛错回滚）；SHALL NOT 以事实表 `MAX(trade_date)` 替代或据其自动推进。一只证券失败 SHALL NOT 阻止同数据集其他证券及其他数据集按各自水位继续。`stock_sync_state` SHALL 同时维护每证券 `ts_code`（最近一次成功同步时的主档规范代码，仅供展示/日志，不作为逻辑键——证券身份以 `instrument_id` 为准，代码变更下水位连续）、`last_task_id`、`last_status`（success/failed）、`last_error_code`/`last_error`、`last_success_at`、`last_attempt_at`；该表 SHALL NOT 建立 UNIQUE 约束、外键与二级索引（项目 DuckDB 惯例），唯一性由 WriteCoordinator 写锁内 get-or-create 保证并经测试守护。`history_sync_state` 的旧数据集水位字段（latest_complete_trade_date/current_trade_date/current_attempt 等）SHALL 冻结于迁移前值（不再推进、不删除，保留历史展示兼容），SHALL NOT 作为个股同步依据。主档数据集（stock_basic/trade_cal/namechange/stock_company/etf_basic）SHALL 继续使用"最近成功刷新状态"语义（bootstrap_complete/master_cursor），SHALL NOT 人为制造交易日或个股水位。

#### Scenario: 各股水位独立

- **WHEN** 一次运行中某数据集 5999 只股票成功、1 只失败
- **THEN** 5999 只各自推进水位，失败股水位不动，其他数据集不受影响

#### Scenario: 证券代码变更水位连续

- **WHEN** 某证券因代码变更（如 `000022.SZ` → `001872.SZ`）在主档中仍指向同一 `instrument_id`
- **THEN** 其 `stock_sync_state` 行不变、水位延续，SHALL NOT 因 ts_code 变化从零重置或产生孤儿状态行

#### Scenario: MAX 日期不推进水位

- **WHEN** 某股事实表中存在晚于其水位的日期（如旧机制遗留）
- **THEN** 该股水位不因此变化，下次同步仍从水位之后的有效区间开始并以区间替换覆盖

#### Scenario: 水位绝不回退

- **WHEN** 提交事务中出现新水位早于该股既有水位
- **THEN** 事务抛错回滚，该股水位保持原值

### Requirement: 数据校验规则

Provider 边界 SHALL 校验上游返回结构可解析、必要字段存在、原始键与数值可转换；Domain 校验（`app/services/history/validation.py` 的 `validate_batch`，仅针对内部标准模型）SHALL 支持单日模式与区间模式两种调用：单日模式（既有按 trade_date 接口沿用）检查全部记录日期与请求日期一致；区间模式（个股区间拉取）检查全部记录日期 ∈ [start_date, end_date] 且不早于该证券 list_date、不晚于 min(end_date, delist_date)。两种模式共同检查：instrument_id/trade_date 非空、批内 `(instrument_id, trade_date)` 不重复、`truncation_risk=False`、无数值 NaN/Inf、instrument 可映射至主档（按数据集分派：股票数据集查 `cn_stock_basic`、ETF 数据集查 `cn_etf_basic`），及各数据集专项规则（daily：OHLC/vol/amount 非负且 high>=max(open,close,low)、low<=min(open,close)、ah_* 可 NULL；adj_factor：每条 >0；daily_basic：估值字段允许 NULL、非 NULL 时股本/市值非负、limit_status 在枚举范围；moneyflow：买卖量额非 NULL 时非负、net_mf_* 允许负数、SHALL NOT 自行计算替代官方净流入字段；etf_daily：OHLC 非负且 high>=max(open,close)、low<=min(open,close)、volume/amount/turnover_rate 非负、NULL 原样保留；etf_adj_factor：每条 >0）。单日模式下 0 行 SHALL 判为校验失败（EMPTY_RESULT，沿用既有行为）；区间模式下 0 行 SHALL NOT 判为校验失败（空区间合法性见"个股空结果语义"）。

#### Scenario: 区间外日期拒绝

- **WHEN** 个股区间请求的返回记录中存在 trade_date 落在 [start_date, end_date] 之外的行
- **THEN** 校验失败（TRADE_DATE_MISMATCH），该股该次尝试不提交

#### Scenario: 上市前日期拒绝

- **WHEN** 返回记录中存在早于该股 list_date 的行
- **THEN** 校验失败，该股该次尝试不提交

#### Scenario: 生命周期内多日行合法

- **WHEN** 个股区间请求返回同一 instrument_id 的多个不同交易日各一行
- **THEN** 校验通过（批内 (instrument_id, trade_date) 无重复即合法），不被误判为重复

#### Scenario: 复权因子非正拒绝

- **WHEN** adj_factor 某记录值 <= 0
- **THEN** 校验失败（INVALID_VALUE），该股该次尝试不提交

#### Scenario: ETF 日线非法 OHLC 拒绝

- **WHEN** etf_daily 某记录 high < max(open, close) 或 volume/amount/turnover_rate 为负
- **THEN** 校验失败（INVALID_VALUE），该 ETF 该次尝试不提交

#### Scenario: ETF 复权因子非正拒绝

- **WHEN** etf_adj_factor 某记录值 <= 0
- **THEN** 校验失败（INVALID_VALUE），该 ETF 该次尝试不提交

#### Scenario: 亏损股 PE NULL 合法

- **WHEN** daily_basic 某证券 pe/pe_ttm 为 NULL
- **THEN** 校验通过并按 NULL 落库，不判为数据缺失

### Requirement: 同步执行记录

系统 SHALL 以三层结构持久化每次执行：`history_sync_run`（run_id 主键；trigger_type：SCHEDULED/MANUAL/STARTUP；status：RUNNING/SUCCESS/FAILED/INTERRUPTED/NOOP——SUCCESS 表示正常执行完毕且允许存在个股 task failed，FAILED 仅表示系统级错误，PARTIAL 枚举保留用于历史行兼容、SHALL NOT 再产生）；`history_sync_run_dataset`（(run_id, dataset) 主键，新增 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count` 统计列；`rows_written`/`request_count`/`retry_count` 继续按个股区间口径累计；旧列 start_watermark/target_trade_date/end_watermark/dates_completed/failed_trade_date 冻结为兼容列、新写入置 NULL/0）；`sync_task`（个股任务流水：id 为 BIGINT 由显式 sequence `seq_sync_task_id` 生成；每次某数据集某股票实际启动一次同步即新增一行，SHALL NOT 覆盖或复用历史行；记录 run_id、dataset、instrument_id、ts_code、start_date/end_date、status（running/success/failed/interrupted）、retry_count（0~max_retries）、attempt_count（1~max_retries+1）、records_fetched/records_written、error_code/error_type/error_message（脱敏、无 traceback）、started_at/finished_at/duration_ms；SHALL NOT 建立 UNIQUE/外键/索引）。`stock_sync_state` SHALL 维护每股 `last_status`（success/failed）与 `last_task_id`。`history_sync_state` SHALL 继续维护数据集级运营字段：日级数据集的 `status` SHALL 由个股口径派生（存在落后股票 → LAGGING、全部追平 → CAUGHT_UP、本轮系统级失败 → FAILED；旧枚举 SYNCING/RETRYING/CHECKING/WAITING_SOURCE 不再产生，保留历史值兼容展示），`last_success_at`/`last_error_code`/`last_error` SHALL 在该数据集段处理完成时刷新（主档数据集语义不变）。dataset 名称 SHALL 为代码层固定常量（stock_basic/trade_cal/namechange/stock_company/daily/adj_factor/daily_basic/moneyflow/etf_basic/etf_daily/etf_adj_factor）。错误 SHALL 标准化为错误码，至少包含：TUSHARE_TOKEN_MISSING、TUSHARE_PERMISSION_DENIED、TUSHARE_RATE_LIMIT、TUSHARE_TIMEOUT、TUSHARE_API_ERROR、EASTMONEY_TIMEOUT、EASTMONEY_API_ERROR、SCHEMA_MISMATCH、TRUNCATION_RISK、DUPLICATE_KEY、TRADE_DATE_MISMATCH、UNKNOWN_INSTRUMENT、ALIAS_CONFLICT、INVALID_VALUE、CALENDAR_UNAVAILABLE、DATABASE_ERROR、INTERNAL_ERROR（EMPTY_RESULT 与 WAITING_SOURCE 的定义保留用于历史数据展示兼容，个股路径不再产生）；错误文本 SHALL NOT 包含 Token 或敏感配置。运行中实时进度（当前 dataset/ts_code、已处理/成功/失败/跳过）SHALL 由 Service 持有的进程内快照暴露给 API，聚合计数以数据库为准。

#### Scenario: 个股失败不影响 Run 成功语义

- **WHEN** 一次 run 中三个数据集全部股票追平、moneyflow 有 2 只股票 task failed
- **THEN** run.status=SUCCESS，moneyflow 的 run_dataset 行 task_failed_count=2，失败详情可在 sync_task 查询

#### Scenario: 无事可做

- **WHEN** 所有数据集全部股票已追平时触发同步
- **THEN** run.status=NOOP，不写事实数据，不创建个股 task

#### Scenario: 任务流水不覆盖

- **WHEN** 同一股票同一数据集先后经历一次 failed 与一次 success
- **THEN** sync_task 存在两条独立记录，stock_sync_state.last_task_id 指向最新一条

#### Scenario: 无工作股票不建任务

- **WHEN** 某股票已追平（watermark 达到有效终点）
- **THEN** 该股不创建 sync_task、不发起网络请求，run_dataset.skipped_count 累计

### Requirement: latest_expected_trade_date（AvailabilityPolicy）

每个日级数据集 SHALL 通过 AvailabilityPolicy 计算自己的当前目标日期：输入当前 Asia/Shanghai 时间、严格交易日历与数据集发布时间规则（默认 cutoff：adj_factor 09:30、daily 16:30、daily_basic 17:30、moneyflow 20:30、etf_daily 16:30、etf_adj_factor 09:30，集中配置可调），输出"此刻理论上应已可获得的最新交易日"。SHALL NOT 以服务器当天日期统一作为目标；盘中/周末手动触发 SHALL 将目标定为最近一个已到发布时间的 open day 且不判为失败。时间基准 SHALL 统一使用 `BUSINESS_TZ_NAME = "Asia/Shanghai"`，与服务器部署时区无关。

#### Scenario: 交易日上午触发

- **WHEN** 交易日上午 10:00 手动触发同步
- **THEN** daily/daily_basic/moneyflow/etf_daily 目标为上一交易日，adj_factor/etf_adj_factor 目标可为今日（已过 09:30）

#### Scenario: 时区无关

- **WHEN** 服务器时区为 UTC 与 Asia/Shanghai 分别注入同一时刻
- **THEN** AvailabilityPolicy 计算的目标日期一致

## ADDED Requirements

### Requirement: ETF 数据集同步编排

ETF 日级数据集（`etf_daily`、`etf_adj_factor`）SHALL 完全复用个股水位引擎：`stock_sync_state` 以 `(dataset, instrument_id)` 为逻辑唯一键容纳 ETF 数据集（SHALL NOT 新建 etf_sync_state 或任何 ETF 专属水位表）；单券任务生命周期、`StockSyncExecutor` 三段写锁事务、失败隔离（单 ETF 重试耗尽只失败自己、不阻塞其他 ETF、不使 Run 失败）、落后水位自动补偿、空结果推进水位、`recover_stale_runs` 恢复语义、重试与配置类错误分类 SHALL 全部沿用既有 requirement，不做第二套实现。两个 ETF 数据集 SHALL 各自独立维护水位、独立推进（etf_adj_factor 数据源故障 SHALL NOT 影响 etf_daily 的同步与水位）；处理顺序 SHALL 为股票四个日级数据集之后执行 etf_daily、再 etf_adj_factor。ETF 待同步集合（universe）SHALL 为 `instrument` 表中 market='CN' AND asset_type='ETF' 的全部记录（含 is_active=false），生命周期取自 `cn_etf_basic`。同步入口 SHALL 复用 `HistorySyncService.run` 单一入口与全部既有触发方式（定时/启动补齐/手动），SHALL NOT 为 ETF 新建独立调度或互斥机制。`history.etf_enabled=false` 时 Run SHALL 完全跳过 ETF 段（含 etf_basic 刷新），不产生任何 ETF 数据集状态与请求。

#### Scenario: 单 ETF 失败隔离

- **WHEN** 一次运行中 etf_daily 有 1000 只 ETF 成功、1 只重试耗尽失败
- **THEN** 1000 只各自推进水位，失败 ETF 水位不动、下轮自动补偿，Run 正常完成且 run_dataset.task_failed_count=1

#### Scenario: 两数据集独立推进

- **WHEN** etf_adj_factor 因 Tushare fund_adj 不可用全部失败、etf_daily 正常
- **THEN** etf_daily 全部 ETF 水位正常推进，etf_adj_factor 水位不动，两者的 stock_sync_state 行互不影响

#### Scenario: 复用水位表无新表

- **WHEN** ETF 首轮同步执行
- **THEN** 在既有 stock_sync_state 表按 (etf_daily, instrument_id)/(etf_adj_factor, instrument_id) 补建状态行，数据库不存在 etf_sync_state 表

#### Scenario: 关闭开关跳过 ETF 段

- **WHEN** history.etf_enabled=false 时触发同步
- **THEN** Run 只处理股票数据集，etf_basic/etf_daily/etf_adj_factor 不出现在本轮 run_dataset，零 ETF 网络请求

### Requirement: ETF universe 刷新为非硬前置

ETF universe（`etf_basic` 数据集）SHALL 在每次 Run 中按 `history.etf_universe_refresh_hours`（默认 24）周期刷新：上次成功刷新不足周期时本轮跳过刷新、数据集状态保持。刷新 SHALL 一次请求获取当前上市 ETF 全集并按"ETF universe 主档同步映射"（instrument-management）upsert。刷新失败 SHALL 仅令 etf_basic 记 FAILED 并将本轮 etf_daily/etf_adj_factor 两个数据集段记 FAILED（零网络请求），SHALL NOT 终止 Run、SHALL NOT 影响股票数据集段（股票段照常推进）、Run 整体 SHALL 仍可 SUCCESS（ETF 数据集级失败由 `history_sync_state` 与 overall_status 表达）。ETF universe 为空（从未成功刷新）时 etf_daily/etf_adj_factor SHALL 判定无 universe 可依、本轮记 FAILED 不推进，SHALL NOT 用空 universe 制造"全部追平"假象。

#### Scenario: universe 刷新失败不终止 Run

- **WHEN** 东财列表接口超时导致 etf_basic 刷新失败，而股票主档与数据集全部正常
- **THEN** Run 完成且 status=SUCCESS，etf_basic/etf_daily/etf_adj_factor 在本轮 run_dataset 记 FAILED，股票数据集统计正常

#### Scenario: 周期未到跳过刷新

- **WHEN** 上次 universe 成功刷新距今 2 小时，本轮 Run 触发
- **THEN** 本轮不发起列表请求（零请求），ETF 数据集段按既有 universe 正常逐券同步

#### Scenario: 空 universe 不伪造追平

- **WHEN** etf_basic 从未成功刷新（cn_etf_basic 为空）时触发 Run
- **THEN** etf_daily/etf_adj_factor 记 FAILED 且 completion 相关统计不输出"100% 追平"假象

### Requirement: ETF 生命周期与空结果语义

单只 ETF 有效同步范围 SHALL 复用生命周期边界规则：有效起点 = `max(history.start_date, list_date)`（cn_etf_basic.list_date 为 NULL 时保守取 history.start_date）；有效终点上界 = `min(数据集 target, delist_date)`（delist_date 缺失取 target）。请求区间端点 SHALL 收敛到严格交易日历。退市 ETF（universe 列表消失后 is_active=false 且无 delist_date）SHALL 继续按 target 推进：请求空结果合法推进水位，追平后零请求（SHALL NOT 悬挂永久缺口）。停牌/无交易 ETF 区间请求成功返回 0 行 SHALL 推进水位（records_fetched=0）；fund_adj 从未覆盖的 ETF 区间 0 行 SHALL 同样合法。ETF 生命周期与空结果语义 SHALL 经在线 smoke 实测确认并固化进离线测试 fake 基线。

#### Scenario: 上市日期缺失保守回退

- **WHEN** 某 ETF cn_etf_basic.list_date 为 NULL 且 history.start_date=2010-01-01
- **THEN** 该 ETF 首次同步从 2010-01-01（或其后第一个严格交易日）开始，不判失败

#### Scenario: 退市 ETF 空结果追平后零请求

- **WHEN** 某 ETF 已从 universe 列表消失（is_active=false、无 delist_date），东财历史接口对其返回 0 行且无异常
- **THEN** 该 ETF 水位推进到 target 后本轮任务成功，下一轮对其零请求，不产生永久缺口告警

#### Scenario: 停牌 ETF 空结果合法

- **WHEN** 某 ETF 请求区间全部为停牌期、etf_daily 返回 0 行且无异常
- **THEN** 水位推进到区间终点，task 记录 records_fetched=0，不判失败不重试
