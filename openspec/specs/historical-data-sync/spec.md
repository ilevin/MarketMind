# historical-data-sync Specification

## Purpose
TBD - created by archiving change a-share-historical-data. Update Purpose after archive.
## Requirements
### Requirement: 四数据集独立连续水位线

`daily`、`adj_factor`、`daily_basic`、`moneyflow` 四个日级数据集 SHALL 在 `history_sync_state` 中各自独立维护 `latest_complete_trade_date`（含义：从历史起点 2010-01-01 起所有应处理交易日已连续、按顺序完整成功到该日）。水位 SHALL 仅代表连续完成进度，SHALL NOT 以 `MAX(trade_date)` 替代或据其自动推进；一个数据集失败 SHALL NOT 阻止其他数据集按各自水位继续。主档数据集（stock_basic/trade_cal/namechange/stock_company）SHALL 使用"最近成功刷新状态"语义（bootstrap_complete/master_cursor），SHALL NOT 人为制造交易日水位。

#### Scenario: 各数据集水位独立

- **WHEN** 一次运行中 daily 与 adj_factor 追平而 moneyflow 停在中间日期失败
- **THEN** 三者 state 分别记录各自水位，run 状态为 PARTIAL，成功数据集不被回滚

#### Scenario: MAX 日期不推进水位

- **WHEN** 事实表中存在晚于水位的日期（如旧脚本遗留）
- **THEN** 水位不因此变化，Planner 仍从水位下一交易日开始逐日处理并整日替换

### Requirement: 严格交易日历推进

日级数据集 SHALL 依据严格交易日历（`trading_calendar` 中 market='CN'、source='tushare'、is_open=1）推进：首次无水位时从 2010-01-01 起的第一个 open day 开始；已有水位时按 `trade_date > 水位 AND trade_date <= target` 升序得到完整待处理列表并逐日处理。SHALL NOT 使用 `date + 1` 或周一至周五近似推进；Tushare Token 缺失或日历无法确认时同步 SHALL 失败/等待而非降级。交易日历 SHALL 按 2010-01-01 至当前年度末分年份获取，缺失或来源非严格 Tushare 的当前年度 SHALL 重新拉取。

#### Scenario: 停机一年后完整补齐

- **WHEN** 水位为 2025-09-01，恢复运行时目标为 2026-09-16
- **THEN** 待处理列表为 2025-09-02 至 2026-09-16 间全部交易日（按日历，排除节假日），逐日顺序处理，不直接跳到最新日

#### Scenario: 春节休市不产生缺口

- **WHEN** 水位后的自然日包含春节长假
- **THEN** 仅交易日历 is_open=1 的日期进入待处理列表，休市日不被判为缺失

### Requirement: 失败日期绝不跳过

某数据集某交易日处理失败（含校验失败、空结果、截断不可确认）时，SHALL 在单次任务内按退避策略最多重试 10 次；10 次仍失败 SHALL 将该数据集标记 FAILED（记录 failed_trade_date 与最后错误）并停止该数据集本次推进，SHALL NOT 跳到后续日期；下一次定时/手动执行 SHALL 从该失败日期继续。重试退避 SHALL 为 `min(5 × 2^(attempt-1), 300)` 秒并施加 0.8~1.2 随机抖动，全部参数可配置；配置类错误（Token 缺失、权限拒绝、schema 不匹配、别名冲突）SHALL 快速失败并显示明确原因，SHALL NOT 无意义睡满 10 轮。

`ALIAS_CONFLICT`（同一规范证券的新旧 ts_code 在同一交易日给出不一致业务字段）SHALL 归入配置类错误：重复请求上游不会改变结果，必须由人工用权威来源判定哪一套数据正确，因此 SHALL 在首次尝试即判定该交易日失败、写入 `last_error_code=ALIAS_CONFLICT` 与 `failed_trade_date`，水位 SHALL NOT 推进。

#### Scenario: 中间日期失败不越过

- **WHEN** 01-04 成功、01-05 连续 10 次失败
- **THEN** 水位停在 01-04，01-06 从未被请求，run_dataset 记录失败日期与错误

#### Scenario: 下次从失败日恢复

- **WHEN** 上述失败后的下一次运行且 01-05 可成功
- **THEN** 从 01-05 继续并追平至目标日期，最终连续完整

#### Scenario: 退避间隔递增有上限

- **WHEN** 观察连续重试的等待时长
- **THEN** 依次约为 5/10/20/40/80/160 秒后封顶 300 秒（含抖动），单日期总尝试次数不超过 10

#### Scenario: 别名冲突快速失败且不推进水位

- **WHEN** 某交易日 daily 返回 `000022.SZ` 与 `001872.SZ` 两行且业务字段不一致
- **THEN** 该数据集 `last_error_code=ALIAS_CONFLICT`、`status=FAILED`、`failed_trade_date` 为该交易日、`latest_complete_trade_date` 保持不变，且该交易日 SHALL 只请求上游一次（不睡满 10 轮退避）

#### Scenario: 别名冲突不影响其余数据集

- **WHEN** daily 因 `ALIAS_CONFLICT` 失败
- **THEN** adj_factor / daily_basic / moneyflow 三个数据集 SHALL NOT 被阻塞，各自按自身水位继续推进

### Requirement: 单日原子提交与幂等

每个数据集每个交易日的落库 SHALL 为一个原子事务（在 WriteCoordinator 写锁内）：查询旧行数 → DELETE 当日旧行 → 批量 INSERT 完整数据 → 写 `history_day_status`=COMPLETE → 更新 `history_sync_state`（水位/计数/状态）→ 更新 `history_sync_run_dataset` → COMMIT。任何一步失败 SHALL 整体回滚（旧数据保留、水位不动）。网络请求、normalize、校验 SHALL 在写锁与事务之外完成。重复执行同一日期 SHALL 以整日 DELETE + INSERT 替换，不产生重复事实记录，且能吸收上游对历史日期的修订。

#### Scenario: 事务中途中断回滚

- **WHEN** DELETE 后 INSERT 过程中抛出异常
- **THEN** 事务回滚，该日旧事实原样保留，水位与 ledger 未变化

#### Scenario: 重复运行不重复计数

- **WHEN** 同一交易日成功同步两次
- **THEN** 事实表该日行数不变，record_count 不翻倍，history_day_status 仍只有一行

#### Scenario: 网络不在写锁内

- **WHEN** 首次历史回填运行
- **THEN** 仅每个交易日落库的短事务占用 WriteCoordinator，Tushare 请求与等待期间其他写操作（自选/行情刷新）可正常获得写锁

### Requirement: 空结果与等待数据源

严格交易日的日级数据集返回 0 行时 SHALL NOT 自动视为成功：明显早于当前日期的空结果 SHALL 按 EMPTY_RESULT 错误码进入重试（10 次后 FAILED、水位不动）；接近发布时间的当日空结果 SHALL 表现为 WAITING_SOURCE（本轮不推进，下次任务继续）。停牌证券无 daily 行、moneyflow 不覆盖某证券、字段 NULL 等证券级自然缺失 SHALL NOT 被判定为日期级不完整，SHALL NOT 以"当日上市证券数量"校验 daily 完整性。

#### Scenario: 历史日期空结果失败

- **WHEN** 请求 2015 年某交易日 daily 返回 0 行且重试后仍为 0
- **THEN** 该数据集本轮标记 FAILED，水位不推进

#### Scenario: 当日未发布等待

- **WHEN** 15:10 管理员手动触发且 daily 当日数据尚未生成
- **THEN** daily 目标仍为上一交易日，当日不计为失败，页面不显示错误

### Requirement: 数据校验规则

Provider 边界 SHALL 校验 Tushare 返回结构可解析、必要字段存在、原始键与数值可转换；Domain 校验（HistoryValidationService，仅针对内部标准模型） SHALL 检查：instrument_id/trade_date 非空、全部记录日期与请求日期一致、`(instrument_id, trade_date)` 不重复、非零行、`truncation_risk=False`、无数值 NaN/Inf、instrument 可映射至主档，及各数据集专项规则（daily：OHLC/vol/amount 非负且 high>=max(open,close,low)、low<=min(open,close)、ah_* 可 NULL；adj_factor：每条 >0 且 ts_code 不重复；daily_basic：估值字段允许 NULL、非 NULL 时股本/市值非负、limit_status 在枚举范围；moneyflow：买卖量额非 NULL 时非负、net_mf_* 允许负数、SHALL NOT 自行计算替代官方净流入字段）。

#### Scenario: 日期不一致拒绝

- **WHEN** 返回记录中存在 trade_date 与请求日期不同的行
- **THEN** 校验失败（TRADE_DATE_MISMATCH），该日不提交

#### Scenario: 复权因子非正拒绝

- **WHEN** adj_factor 某记录值 <= 0
- **THEN** 校验失败（INVALID_VALUE），该日不提交

#### Scenario: 亏损股 PE NULL 合法

- **WHEN** daily_basic 某证券 pe/pe_ttm 为 NULL
- **THEN** 校验通过并按 NULL 落库，不判为数据缺失

### Requirement: 水位一致性对账

每次统一任务开始时 SHALL 对四个日级数据集执行轻量 reconcile：水位存在时该日期 ledger 应为 COMPLETE、从起点到水位的交易日均有 COMPLETE 账本、事实表 MAX(trade_date) 与 state 无明显矛盾（矛盾仅用于发现异常，不用于推进水位）。发现不一致时 SHALL 保守恢复：按严格日历扫描 `history_day_status` 找到第一个缺失 COMPLETE 的交易日，将水位回退到其前一交易日并从缺失日重新同步。Service SHALL 提供内部 `reconcile_dataset(dataset)` 诊断能力；第一阶段管理员页面 SHALL NOT 提供水位手工输入或修复按钮。

#### Scenario: 账本缺口的保守回退

- **WHEN** 水位为 09-10 但 history_day_status 缺 09-08 的 COMPLETE 记录
- **THEN** 水位回退到 09-07，从 09-08 重新同步，不依据更晚的事实日期跳过缺口

### Requirement: latest_expected_trade_date（AvailabilityPolicy）

每个日级数据集 SHALL 通过 AvailabilityPolicy 计算自己的当前目标日期：输入当前 Asia/Shanghai 时间、严格交易日历与数据集发布时间规则（默认 cutoff：adj_factor 09:30、daily 16:30、daily_basic 17:30、moneyflow 20:30，集中配置可调），输出"此刻理论上应已可获得的最新交易日"。SHALL NOT 以服务器当天日期统一作为目标；盘中/周末手动触发 SHALL 将目标定为最近一个已到发布时间的 open day 且不判为失败。时间基准 SHALL 统一使用 `BUSINESS_TZ_NAME = "Asia/Shanghai"`，与服务器部署时区无关。

#### Scenario: 交易日上午触发

- **WHEN** 交易日上午 10:00 手动触发同步
- **THEN** daily/daily_basic/moneyflow 目标为上一交易日，adj_factor 目标可为今日（已过 09:30）

#### Scenario: 时区无关

- **WHEN** 服务器时区为 UTC 与 Asia/Shanghai 分别注入同一时刻
- **THEN** AvailabilityPolicy 计算的目标日期一致

### Requirement: 主档前置与刷新周期

统一同步 SHALL 先完成硬前置（trade_cal 缺失即刷新、stock_basic 超过 24 小时未成功则刷新），再刷新非阻塞主档（stock_company、namechange 默认每 7 天，namechange 增量带 7 天重叠窗口以吸收迟到修订，bootstrap 逐只推进 master_cursor 可中断续跑），最后执行四个日级数据集。主档刷新失败时：trade_cal/stock_basic 失败 SHALL 阻止日级数据集推进；stock_company/namechange 失败 SHALL NOT 阻塞日级数据集。namechange 重叠窗口写入 SHALL 删除窗口内旧事件后插入完整返回集、保留更早历史。

#### Scenario: 硬前置失败阻止日级推进

- **WHEN** trade_cal 严格刷新失败
- **THEN** 四个日级数据集本轮不推进，run 记录失败原因（CALENDAR_UNAVAILABLE）

#### Scenario: namechange 增量重叠窗口

- **WHEN** namechange 增量刷新（start_date=上次成功-7天）
- **THEN** 窗口内旧事件被替换为当前完整返回，窗口外历史事件保留

### Requirement: 统一入口与触发方式

历史同步 SHALL 只有单一业务入口 `HistorySyncService.run(trigger, requested_by)`，定时（SCHEDULED，默认每天 20:30 Asia/Shanghai、含周末）、启动 catch-up（STARTUP，发现落后即触发）与管理员手动（MANUAL）三种触发来源 SHALL 复用同一流程（检查、目标计算、水位读取、逐日补数、校验、写入、重试、状态更新）。每日定时 SHALL 每天运行（周末不产生虚假交易日且可追平周五缺口）。

#### Scenario: 手动与定时同逻辑

- **WHEN** 管理员点击"检查并更新"与定时任务分别触发
- **THEN** 两者执行同一 Service 代码路径，行为一致

#### Scenario: 启动自动补落后

- **WHEN** 应用启动且某数据集落后当前目标
- **THEN** 自动触发一次 STARTUP 同步，不等次日 20:30

### Requirement: 任务互斥（single-flight）

统一历史同步 SHALL 进程级互斥：已有任务运行时，定时触发 SHALL 记录 skip 不重复启动；管理员 API 触发 SHALL 返回 409 与当前 run_id；SHALL NOT 出现两个同步同时推进水位。

#### Scenario: 运行中重复触发

- **WHEN** 同步运行中再次 POST /api/admin/history-data/sync
- **THEN** 返回 409 与正在运行的 run_id，不产生第二个 run

### Requirement: 进程中断与恢复

应用启动时 SHALL 将遗留 status=RUNNING 的 history_sync_run 标记为 INTERRUPTED，并把 SYNCING/RETRYING/CHECKING 状态的数据集 state 恢复为基于水位的 LAGGING 或 CAUGHT_UP；恢复 SHALL NOT 依据"run 曾经 RUNNING"猜测某日已完成，完成依据仍为 history_day_status + latest_complete_trade_date。同步 SHALL 接受 cancellation_event 并在每交易日开始前、每次重试 sleep 前后、每个 master 分片之间检查；收到停止时当前事务允许正常完成、不开始下一日，run 标记 INTERRUPTED 或由下次启动恢复。

#### Scenario: 重启后恢复

- **WHEN** 上次进程在同步中退出，重启应用
- **THEN** 旧 run 标记 INTERRUPTED，数据集状态恢复，startup catch-up 从水位继续，无重复推进

#### Scenario: 优雅停机

- **WHEN** 停机信号到达且当前正处于某交易日事务中
- **THEN** 当前事务正常完成（含水位推进），后续日期不再开始

### Requirement: 同步执行记录

系统 SHALL 以 `history_sync_run`（run_id 主键；trigger_type：SCHEDULED/MANUAL/STARTUP；status：RUNNING/SUCCESS/PARTIAL/FAILED/INTERRUPTED/NOOP）与 `history_sync_run_dataset`（(run_id, dataset) 主键，记录起止水位/目标、游标、dates_completed、rows_written、request_count、retry_count、failed_trade_date 与最后错误）持久化每次执行；`history_sync_state` SHALL 维护数据集状态枚举（UNINITIALIZED/CHECKING/SYNCING/RETRYING/CAUGHT_UP/LAGGING/FAILED/WAITING_SOURCE）与 dataset_kind（DAILY_CONTIGUOUS/MASTER）、last_success_at、last_error_code/last_error 等。dataset 名称 SHALL 为代码层固定常量（stock_basic/trade_cal/namechange/stock_company/daily/adj_factor/daily_basic/moneyflow）。错误 SHALL 标准化为错误码，至少包含：TUSHARE_TOKEN_MISSING、TUSHARE_PERMISSION_DENIED、TUSHARE_RATE_LIMIT、TUSHARE_TIMEOUT、TUSHARE_API_ERROR、EMPTY_RESULT、TRUNCATION_RISK、SCHEMA_MISMATCH、DUPLICATE_KEY、TRADE_DATE_MISMATCH、UNKNOWN_INSTRUMENT、INVALID_VALUE、CALENDAR_UNAVAILABLE、DATABASE_ERROR、INTERNAL_ERROR；错误文本 SHALL NOT 包含 Token 或敏感配置。

#### Scenario: 部分成功可追溯

- **WHEN** 一次 run 中三个数据集追平、moneyflow 失败
- **THEN** run.status=PARTIAL，run_dataset 各行分别记录推进/失败详情，错误码可查

#### Scenario: 无事可做

- **WHEN** 所有数据集已追平时触发同步
- **THEN** run.status=NOOP，不写事实数据

### Requirement: 同步日志规范

历史同步日志 SHALL 携带结构化上下文：run_id、dataset、trade_date、attempt、row_count、elapsed_ms，失败时含 error_code。日志级别 SHALL 遵循：正常完成 INFO、重试 WARNING、10 次失败 ERROR。单日几千行事实数据 SHALL NOT 逐行打印，首次回填期间每个交易日输出一条概要日志即可。日志与错误文本 SHALL NOT 包含 Tushare Token 或敏感配置。

#### Scenario: 回填期间按日概要

- **WHEN** 首次回填成功同步某交易日约 5000 行 daily
- **THEN** 输出一条含 run_id/dataset/trade_date/row_count/elapsed_ms 的 INFO 概要日志，不逐行打印事实数据

#### Scenario: 重试与耗尽告警

- **WHEN** 某交易日第 3 次尝试仍失败、第 10 次尝试后仍失败
- **THEN** 分别记录 WARNING（含 attempt 与 error_code）与 ERROR 日志

#### Scenario: 日志不含 Token

- **WHEN** 同步因 Token 配置错误失败
- **THEN** 日志与错误文本仅含错误码与脱敏描述，不含 Token 明文

### Requirement: 批量写入性能

事实表写入 SHALL 使用 SQLAlchemy Core executemany（`session.execute(fact_table.insert(), records)`，可按 1000~2000 行分 chunk 但保持同日同事务），SHALL NOT 对大事实表逐行 ORM insert。后续性能优化 SHALL 只允许替换 Repository 内部批量写实现（如 DuckDB staging + INSERT SELECT），SHALL NOT 破坏整日原子提交语义。系统 SHALL 提供本地 synthetic 基准（约 6000 行 × 100 交易日）验证未退化为逐行写入。

#### Scenario: 单日批量写入

- **WHEN** 同步某交易日约 5300 行 daily 数据
- **THEN** 以 Core executemany（或分 chunk）一次事务完成，耗时与行数呈批量线性关系而非逐行开销

