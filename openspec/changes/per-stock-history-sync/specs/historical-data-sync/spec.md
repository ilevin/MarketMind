## RENAMED Requirements

- FROM: `### Requirement: 四数据集独立连续水位线`
  TO: `### Requirement: 个股独立连续水位线`
- FROM: `### Requirement: 失败日期绝不跳过`
  TO: `### Requirement: 个股失败隔离与缺口不跳过`
- FROM: `### Requirement: 单日原子提交与幂等`
  TO: `### Requirement: 单股区间原子提交与幂等`
- FROM: `### Requirement: 空结果与等待数据源`
  TO: `### Requirement: 个股空结果语义`

## REMOVED Requirements

### Requirement: 水位一致性对账
**Reason**: 该 requirement 的对账对象是"数据集级 `latest_complete_trade_date` 与 `history_day_status` 日账本"的跨表一致性，随日级整日水位模型整体退役。个股水位的正确性由结构保证——水位推进与事实写入位于同一原子事务（见"单股区间原子提交与幂等"），不存在需要事后对账的两套来源；缺口自愈由落后水位检测（见"自动补偿"）完成。
**Migration**: `reconcile_daily_watermarks`/`reconcile_dataset` 随日级单日路径一并移除；`history_day_status` 账本停写（历史行保留只读，供旧执行记录追溯）；管理员页面继续不提供水位手工输入或修复按钮。

## MODIFIED Requirements

### Requirement: 个股独立连续水位线

`daily`、`adj_factor`、`daily_basic`、`moneyflow` 四个日级数据集 SHALL 在 `stock_sync_state` 表中以 `(dataset, instrument_id)` 为逻辑唯一键，为每只股票独立维护 `watermark_date`（含义：该股从有效同步起点起截至该日的应同步数据已连续、完整成功；NULL 表示尚未确认任何边界，从有效起点全量同步）。水位 SHALL 仅在单股区间原子提交成功的同一事务内推进，SHALL 单调不下降（提交事务内校验新水位早于旧水位即抛错回滚）；SHALL NOT 以事实表 `MAX(trade_date)` 替代或据其自动推进。一只股票失败 SHALL NOT 阻止同数据集其他股票及其他数据集按各自水位继续。`stock_sync_state` SHALL 同时维护每股 `ts_code`（最近一次成功同步时的主档规范代码，仅供展示/日志，不作为逻辑键——证券身份以 `instrument_id` 为准，代码变更下水位连续）、`last_task_id`、`last_status`（success/failed）、`last_error_code`/`last_error`、`last_success_at`、`last_attempt_at`；该表 SHALL NOT 建立 UNIQUE 约束、外键与二级索引（项目 DuckDB 惯例），唯一性由 WriteCoordinator 写锁内 get-or-create 保证并经测试守护。`history_sync_state` 的旧数据集水位字段（latest_complete_trade_date/current_trade_date/current_attempt 等）SHALL 冻结于迁移前值（不再推进、不删除，保留历史展示兼容），SHALL NOT 作为个股同步依据。主档数据集（stock_basic/trade_cal/namechange/stock_company）SHALL 继续使用"最近成功刷新状态"语义（bootstrap_complete/master_cursor），SHALL NOT 人为制造交易日或个股水位。

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

### Requirement: 个股失败隔离与缺口不跳过

某数据集某股票一次同步失败（含校验失败、网络错误、截断不可确认）时，SHALL 在同一条 `sync_task` 内按退避策略最多重试 `history.max_retries`（默认 3）次、单任务总尝试至多 `max_retries + 1` 次（默认 4 次）；重试耗尽 SHALL 将该 task 标记 failed 并记录最后错误码与脱敏错误文本，该股 `watermark_date` SHALL NOT 推进，但 SHALL NOT 阻止同数据集其他股票继续处理，SHALL NOT 使 Run 整体失败，处理循环 SHALL 继续下一只股票。该股在下次运行（定时/启动补齐/手动）SHALL 依据落后水位自动重新进入待同步队列并从 `watermark_date` 之后继续，SHALL NOT 跳过缺口、SHALL NOT 依赖人工补偿。重试退避 SHALL 复用 `min(5 × 2^(attempt-1), 300)` 秒并施加 0.8~1.2 随机抖动（参数沿用现有配置项）；配置类错误（Token 缺失、权限拒绝、schema 不匹配、别名冲突）SHALL 快速失败——首次尝试即判定该股该任务失败，SHALL NOT 睡满退避轮次。

`ALIAS_CONFLICT`（同一规范证券的新旧 ts_code 在同一交易日给出不一致业务字段）SHALL 归入配置类错误：SHALL 在首次尝试即判定该股该任务失败、记录 `error_code=ALIAS_CONFLICT`，该股水位 SHALL NOT 推进；SHALL NOT 阻塞同数据集其他股票，SHALL NOT 使 Run 失败。

个股之外的系统级异常（数据库不可用、交易日历前置失败、编排框架崩溃）SHALL 不属于个股失败，SHALL 终止整个 Run 并按系统级失败记录。

#### Scenario: 单股失败不阻塞其他股票

- **WHEN** 某数据集轮到某股票后重试耗尽（默认 4 次尝试）全部失败
- **THEN** 该股 task=failed、水位不动，处理循环继续下一只股票，Run 正常完成且 run_dataset.task_failed_count 计 1

#### Scenario: 失败股票下次从缺口继续

- **WHEN** 上述失败后的下一次运行且该股可成功
- **THEN** 该股自动进入待同步队列，从其水位之后的区间继续并追平目标，无需人工干预

#### Scenario: 退避间隔递增有上限

- **WHEN** 观察单股连续重试的等待时长
- **THEN** 依次约为 5/10/20 秒（含 0.8~1.2 抖动，默认配置下），单任务总尝试次数不超过 max_retries + 1

#### Scenario: 别名冲突快速失败该股

- **WHEN** 某股票区间请求返回的数据中，同一交易日新旧 ts_code 业务字段不一致
- **THEN** 该股该任务首次尝试即失败（error_code=ALIAS_CONFLICT）、水位不动、只请求上游一次（不睡满退避轮次），同数据集其他股票与 Run 不受影响

#### Scenario: 系统级异常终止 Run

- **WHEN** 同步过程中数据库连接不可用
- **THEN** Run 标记 FAILED 并终止，未提交股票的水位不动，已成功股票的进度保留

### Requirement: 单股区间原子提交与幂等

每个数据集每只股票一次同步 SHALL 使用少量短事务：任务开始时创建 `sync_task`（status=running，含 run_id、dataset、instrument 标识、请求区间）为一个写锁事务；同步成功时"事实区间替换（DELETE 该股 [start_date, end_date] 旧行 → staging 批量 INSERT）→ 更新 `stock_sync_state`（watermark_date 推进、last_* 刷新）→ 更新 `sync_task`（success 与 records_written）→ 累计 `history_sync_run_dataset` 计数" SHALL 在同一个 WriteCoordinator 写锁事务内原子提交；同步失败（重试耗尽或配置类错误）时"`sync_task` 置 failed（结构化错误）→ `stock_sync_state` 更新 last_status/last_error_*" SHALL 同为一个写锁事务。任何一步失败 SHALL 整体回滚（旧数据保留、水位不动、无中间态残留）。网络请求、normalize、校验 SHALL 在写锁与事务之外完成。重复同步同一区间 SHALL 以区间 DELETE + INSERT 替换，不产生重复事实行，且能吸收上游对历史日期的修订。

#### Scenario: 事务中途中断回滚

- **WHEN** 区间 DELETE 后 INSERT 过程中抛出异常
- **THEN** 事务回滚，该股旧事实原样保留，水位与 task 状态未变化

#### Scenario: 重复运行不重复计数

- **WHEN** 同一股票同一区间成功同步两次
- **THEN** 事实表该区间行数不变，record_count 不翻倍，水位不重复推进

#### Scenario: 网络不在写锁内

- **WHEN** 首次全量个股回填运行
- **THEN** 仅任务创建与结果落库的短事务占用 WriteCoordinator，Tushare 请求与重试等待期间其他写操作（自选/行情刷新）可正常获得写锁

#### Scenario: 任务创建后进程崩溃不留悬空状态

- **WHEN** 某股票 task 创建后、提交事务前进程退出
- **THEN** 下次启动恢复时该 task 被标记 interrupted，该股水位未动，下次运行从原水位重新同步

### Requirement: 个股空结果语义

个股区间请求成功（无异常）返回 0 行时 SHALL 视为上游对区间的有效响应并推进水位：`records_fetched=0` 记入 `sync_task`，输出 WARNING 日志，不判失败、不重试。长期停牌区间、moneyflow 不覆盖的证券、退市前无数据区间均属合法空结果形态。请求抛错（超时、限流、Schema 不匹配等）SHALL 按可重试错误进入正常重试路径。"当日数据未发布" SHALL 由调度端 AvailabilityPolicy 吸收（个股请求的 `end_date` 恒为已过发布 cutoff 的交易日，不会指向未发布日）；个股路径 SHALL NOT 产生 `WAITING_SOURCE` 状态或"历史日期空结果判失败"逻辑，`WAITING_SOURCE`/`EMPTY_RESULT` 的状态与错误码定义 SHALL 保留用于历史数据展示兼容。各数据集空结果语义（停牌股区间、退市股末段、moneyflow 非覆盖股）SHALL 经在线 smoke 实测确认并固化进离线测试的 fake 响应。

#### Scenario: 停牌区间空结果推进水位

- **WHEN** 某股请求区间全部为停牌期、daily 返回 0 行且无异常
- **THEN** 该股水位推进到区间终点，task 记录 records_fetched=0，不判失败

#### Scenario: 退市股末段与非覆盖股合法为空

- **WHEN** 退市股最后区间或 moneyflow 从未覆盖的股票返回 0 行
- **THEN** 水位推进、任务成功，不产生缺口告警

#### Scenario: 请求异常仍进入重试

- **WHEN** 个股区间请求抛出超时或限流异常
- **THEN** 按退避策略重试，重试耗尽该股 task=failed、水位不动

### Requirement: 严格交易日历推进

日级数据集 SHALL 依据严格交易日历（`trading_calendar` 中 market='CN'、source='tushare'、is_open=1）界定每只股票的同步区间：请求起点 SHALL 为 `watermark_date` 之后的第一个严格交易日（无水位时为有效同步起点起的第一个严格交易日），请求终点 SHALL 为有效终点上界（见"股票生命周期边界"）内最近的严格交易日；SHALL NOT 使用 `date + 1` 或周一至周五近似推进。Tushare Token 缺失或日历无法确认时同步 SHALL 失败/等待而非降级。交易日历 SHALL 按 2010-01-01 至当前年度末分年份获取，缺失或来源非严格 Tushare 的当前年度 SHALL 重新拉取。

#### Scenario: 停机一年后一次请求覆盖缺口

- **WHEN** 某股水位为 2025-09-01，恢复运行时目标为 2026-09-16
- **THEN** 该股以一次区间请求覆盖 2025-09-02 至 2026-09-16 间全部交易日（按日历，排除节假日），不逐日请求、不直接跳到最新日

#### Scenario: 春节休市不产生缺口

- **WHEN** 某股水位后的自然日包含春节长假
- **THEN** 仅交易日历 is_open=1 的日期进入有效区间，休市日不被判为缺失

### Requirement: 数据校验规则

Provider 边界 SHALL 校验 Tushare 返回结构可解析、必要字段存在、原始键与数值可转换；Domain 校验（`app/services/history/validation.py` 的 `validate_batch`，仅针对内部标准模型）SHALL 支持单日模式与区间模式两种调用：单日模式（既有按 trade_date 接口沿用）检查全部记录日期与请求日期一致；区间模式（个股区间拉取）检查全部记录日期 ∈ [start_date, end_date] 且不早于该股 list_date、不晚于 min(end_date, delist_date)。两种模式共同检查：instrument_id/trade_date 非空、批内 `(instrument_id, trade_date)` 不重复、`truncation_risk=False`、无数值 NaN/Inf、instrument 可映射至主档，及各数据集专项规则（daily：OHLC/vol/amount 非负且 high>=max(open,close,low)、low<=min(open,close)、ah_* 可 NULL；adj_factor：每条 >0；daily_basic：估值字段允许 NULL、非 NULL 时股本/市值非负、limit_status 在枚举范围；moneyflow：买卖量额非 NULL 时非负、net_mf_* 允许负数、SHALL NOT 自行计算替代官方净流入字段）。单日模式下 0 行 SHALL 判为校验失败（EMPTY_RESULT，沿用既有行为）；区间模式下 0 行 SHALL NOT 判为校验失败（空区间合法性见"个股空结果语义"）。

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

#### Scenario: 亏损股 PE NULL 合法

- **WHEN** daily_basic 某证券 pe/pe_ttm 为 NULL
- **THEN** 校验通过并按 NULL 落库，不判为数据缺失

### Requirement: 统一入口与触发方式

历史同步 SHALL 只有单一业务入口 `HistorySyncService.run(trigger, requested_by)`，定时（SCHEDULED，默认每天 20:30 Asia/Shanghai、含周末）、启动 catch-up（STARTUP，发现落后即触发）与管理员手动（MANUAL）三种触发来源 SHALL 复用同一流程（检查、目标计算、个股水位读取、逐股补数、校验、写入、重试、状态更新）。每日定时 SHALL 每天运行（周末不产生虚假交易日且可追平周五缺口）。

#### Scenario: 手动与定时同逻辑

- **WHEN** 管理员点击"检查并更新"与定时任务分别触发
- **THEN** 两者执行同一 Service 代码路径，行为一致

#### Scenario: 启动自动补落后

- **WHEN** 应用启动且某数据集存在落后水位的股票
- **THEN** 自动触发一次 STARTUP 同步，不等次日 20:30

### Requirement: 进程中断与恢复

应用启动时 SHALL 将遗留 status=RUNNING 的 history_sync_run 标记为 INTERRUPTED，把属于已中断 Run 的 `sync_task`（status=running）批量标记为 interrupted（补 finished_at），并按各自水位恢复数据集状态；恢复 SHALL NOT 依据"run 曾经 RUNNING"猜测某股已完成，完成依据仍为已提交事实 + `stock_sync_state.watermark_date`；被中断 task 对应股票的水位 SHALL NOT 推进（提交事务未发生），下次运行按原水位重新同步。同步 SHALL 接受 cancellation_event 并在每只股票开始前、每次重试 sleep 前后、每个 master 分片之间检查；收到停止信号时当前股票的事务允许正常完成、不开始下一只股票，Run 标记 INTERRUPTED 或由下次启动恢复。

#### Scenario: 重启后恢复

- **WHEN** 上次进程在个股同步中退出，重启应用
- **THEN** 旧 run 标记 INTERRUPTED、遗留 running 的 task 标记 interrupted，未完成股票水位不动，startup catch-up 从原水位继续，无重复推进

#### Scenario: 优雅停机

- **WHEN** 停机信号到达且当前正处于某股票提交事务中
- **THEN** 当前事务正常完成（含水位推进），后续股票不再开始

### Requirement: 同步执行记录

系统 SHALL 以三层结构持久化每次执行：`history_sync_run`（run_id 主键；trigger_type：SCHEDULED/MANUAL/STARTUP；status：RUNNING/SUCCESS/FAILED/INTERRUPTED/NOOP——SUCCESS 表示正常执行完毕且允许存在个股 task failed，FAILED 仅表示系统级错误，PARTIAL 枚举保留用于历史行兼容、SHALL NOT 再产生）；`history_sync_run_dataset`（(run_id, dataset) 主键，新增 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count` 统计列；`rows_written`/`request_count`/`retry_count` 继续按个股区间口径累计；旧列 start_watermark/target_trade_date/end_watermark/dates_completed/failed_trade_date 冻结为兼容列、新写入置 NULL/0）；`sync_task`（个股任务流水：id 为 BIGINT 由显式 sequence `seq_sync_task_id` 生成；每次某数据集某股票实际启动一次同步即新增一行，SHALL NOT 覆盖或复用历史行；记录 run_id、dataset、instrument_id、ts_code、start_date/end_date、status（running/success/failed/interrupted）、retry_count（0~max_retries）、attempt_count（1~max_retries+1）、records_fetched/records_written、error_code/error_type/error_message（脱敏、无 traceback）、started_at/finished_at/duration_ms；SHALL NOT 建立 UNIQUE/外键/索引）。`stock_sync_state` SHALL 维护每股 `last_status`（success/failed）与 `last_task_id`。`history_sync_state` SHALL 继续维护数据集级运营字段：日级数据集的 `status` SHALL 由个股口径派生（存在落后股票 → LAGGING、全部追平 → CAUGHT_UP、本轮系统级失败 → FAILED；旧枚举 SYNCING/RETRYING/CHECKING/WAITING_SOURCE 不再产生，保留历史值兼容展示），`last_success_at`/`last_error_code`/`last_error` SHALL 在该数据集段处理完成时刷新（主档数据集语义不变）。dataset 名称 SHALL 为代码层固定常量（stock_basic/trade_cal/namechange/stock_company/daily/adj_factor/daily_basic/moneyflow）。错误 SHALL 标准化为错误码，至少包含：TUSHARE_TOKEN_MISSING、TUSHARE_PERMISSION_DENIED、TUSHARE_RATE_LIMIT、TUSHARE_TIMEOUT、TUSHARE_API_ERROR、SCHEMA_MISMATCH、TRUNCATION_RISK、DUPLICATE_KEY、TRADE_DATE_MISMATCH、UNKNOWN_INSTRUMENT、ALIAS_CONFLICT、INVALID_VALUE、CALENDAR_UNAVAILABLE、DATABASE_ERROR、INTERNAL_ERROR（EMPTY_RESULT 与 WAITING_SOURCE 的定义保留用于历史数据展示兼容，个股路径不再产生）；错误文本 SHALL NOT 包含 Token 或敏感配置。运行中实时进度（当前 dataset/ts_code、已处理/成功/失败/跳过）SHALL 由 Service 持有的进程内快照暴露给 API，聚合计数以数据库为准。

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

### Requirement: 同步日志规范

历史同步日志 SHALL 携带结构化上下文：run_id、dataset、ts_code、attempt、row_count、elapsed_ms，失败时含 error_code。日志级别 SHALL 遵循：正常完成 INFO、重试 WARNING、单股重试耗尽 ERROR。单股数千行事实数据 SHALL NOT 逐行打印，全量回填期间每只股票输出一条概要日志即可。日志与错误文本 SHALL NOT 包含 Tushare Token 或敏感配置。

#### Scenario: 回填期间按股概要

- **WHEN** 全量回填成功同步某股票约 4000 行 16 年日线
- **THEN** 输出一条含 run_id/dataset/ts_code/row_count/elapsed_ms 的 INFO 概要日志，不逐行打印事实数据

#### Scenario: 重试与耗尽告警

- **WHEN** 某股票第 2 次尝试仍失败、第 4 次（最后一次）尝试后仍失败
- **THEN** 分别记录 WARNING（含 attempt 与 error_code）与 ERROR 日志

#### Scenario: 日志不含 Token

- **WHEN** 同步因 Token 配置错误失败
- **THEN** 日志与错误文本仅含错误码与脱敏描述，不含 Token 明文

### Requirement: 批量写入性能

事实表写入 SHALL 使用 SQLAlchemy Core executemany 或 DuckDB staging + INSERT SELECT 批量实现（`HistoryFactRepository`，可按 1000~2000 行分 chunk 但保持同股同区间同事务），SHALL NOT 对大事实表逐行 ORM insert。后续性能优化 SHALL 只允许替换 Repository 内部批量写实现，SHALL NOT 破坏单股区间原子提交语义。系统 SHALL 提供本地 synthetic 基准验证未退化为逐行写入：既有整日批量场景（约 6000 行 × 100 交易日）与新增个股区间场景（单股约 4000 行 × 多股连续提交）。

#### Scenario: 单股区间批量写入

- **WHEN** 同步某股票 16 年约 4000 行 daily 数据
- **THEN** 以批量写一次事务完成，耗时与行数呈批量线性关系而非逐行开销

#### Scenario: 多股连续提交不退化

- **WHEN** 基准脚本连续提交多只股票的区间替换事务
- **THEN** 每股提交语句数与事务数保持常量级，不随股票数或行数逐行放大

#### Scenario: 既有整日基准保留

- **WHEN** 基准脚本运行既有整日批量场景（约 6000 行 × 100 交易日）
- **THEN** 仍以批量写完成，耗时与行数呈批量线性关系而非逐行开销

## ADDED Requirements

### Requirement: 股票生命周期边界

个股有效同步范围 SHALL 由证券生命周期界定：有效起点 = `max(history.start_date, list_date)`（list_date 缺失时保守取 history.start_date）；有效终点上界 = `min(数据集 target, delist_date)`（delist_date 缺失取 target）。上市前的交易日 SHALL NOT 计为缺口、SHALL NOT 发起请求；`delist_date` 早于 history.start_date 的退市股 SHALL 判定无工作、不进入待同步处理；退市股在退市前区间内 SHALL 正常同步（历史数据有价值）。待同步股票集合（universe）SHALL 覆盖主档全部上市状态（含退市与暂停上市），SHALL NOT 裁剪到当前在市。

#### Scenario: 中途上市股票不回填上市前

- **WHEN** 某股 list_date=2015-06-12，历史起点 2010-01-01
- **THEN** 该股首次同步从 2015-06-12（或其后第一个严格交易日）开始，2010~2015 不计为缺口

#### Scenario: 退市股票同步至退市日

- **WHEN** 某股 delist_date=2020-08-28（早于当前 target）
- **THEN** 该股有效终点为 2020-08-28 内最近严格交易日，同步完成后永久无工作、不再请求

#### Scenario: 早于历史起点的退市股无工作

- **WHEN** 某股 delist_date=2009-05-01 早于 history.start_date=2010-01-01
- **THEN** 该股被跳过（skipped_count 累计），不创建任务、不发起请求

### Requirement: 自动补偿

系统 SHALL NOT 建立 `sync_retry` 队列表或其他人工补偿机制：每次 Run SHALL 将 `watermark_date` 落后于当前有效终点（含 NULL）的股票自动纳入待同步队列，按 `watermark_date 升序（NULL 视为最旧）、ts_code 升序` 排序处理（落后最久优先）。失败股票 SHALL 在下一次 Run 自动重新参与补偿，无需人工干预；已追平的股票 SHALL 产生零网络请求。

#### Scenario: 失败股自动补偿

- **WHEN** 某股上一轮重试耗尽失败，本轮运行且上游恢复
- **THEN** 该股自动进入待同步队列并成功追平，无需人工操作

#### Scenario: 已追平股票零请求

- **WHEN** 某数据集全部股票已追平时触发 Run
- **THEN** 对已追平股票不发起任何 Tushare 请求

#### Scenario: 落后最久优先

- **WHEN** 数据集内同时存在水位 NULL、2015 年、2026 年的落后股票
- **THEN** 处理顺序为 NULL 先于 2015 先于 2026，同水位按 ts_code 升序

### Requirement: 今日成功/失败统计

"今日成功/失败" SHALL 定义为：`stock_sync_state.last_attempt_at` 转换为 Asia/Shanghai 业务时区日期后等于当天、且 `last_status` 分别为 success/failed 的股票数（即该股该数据集今天最后一次完成任务的状态；同日先失败后成功只计成功，反之只计失败）。统计 SHALL 经 SQL `AT TIME ZONE 'Asia/Shanghai'` 换算完成，与服务器部署时区无关；SHALL 只对 `stock_sync_state` 小表做聚合，SHALL NOT 扫描事实大表。

#### Scenario: 同日先失败后成功计成功

- **WHEN** 某股当天 09:00 一次任务 failed、09:10 下一次任务 success
- **THEN** 该股计入 today_success_count=1，不计入 today_failed_count

#### Scenario: 时区无关

- **WHEN** 服务器部署时区为 UTC，北京时间 23:30 完成一次成功任务
- **THEN** 该任务按 Asia/Shanghai 日期计入"今日成功"，不因 UTC 已跨日而漏计
