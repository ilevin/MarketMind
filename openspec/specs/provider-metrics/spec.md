# provider-metrics Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: Provider 超时配置

Tencent、AKShare、Tushare 三个 Provider SHALL 各自具备明确的超时配置（`providers.timeout.{tencent,akshare,tushare}`，单位秒，默认 8/15/15），经应用配置注入；配置缺省时 SHALL 使用默认值。Tencent 经 HTTP 客户端参数生效，Tushare 经 SDK 参数生效，AKShare 经调用包装层的限时执行生效。

#### Scenario: 配置指定超时
- **WHEN** config.yaml 配置 `providers.timeout.tencent: 5`
- **THEN** Tencent 行情请求以 5 秒为上限

#### Scenario: 配置缺省使用默认值
- **WHEN** config.yaml 无 providers.timeout 节
- **THEN** 三个 Provider 分别按默认超时运行，应用正常启动

### Requirement: Provider 失败与超时后的行为

Provider 调用失败（无论超时还是报错）SHALL 记为本次调用失败并停止等待（后台刷新任务 SHALL NOT 因单个 Provider 无响应或报错而长期卡住）；失败 SHALL 保留最后一次成功行情（Last Known Good），SHALL NOT 删除已有缓存或快照；下一个刷新周期 SHALL 重新尝试。

#### Scenario: 超时不阻塞刷新周期
- **WHEN** AKShare 全市场请求超过超时时间未返回
- **THEN** 该次调用按失败处理，刷新周期正常结束，旧行情继续展示

#### Scenario: 报错不删除旧行情
- **WHEN** Provider 返回 HTTP 500 或解析失败等非超时错误
- **THEN** 该次调用计入 error，已有行情快照与缓存原样保留

#### Scenario: 失败后下一周期恢复
- **WHEN** 某 Provider 本周期超时或报错，下一周期恢复正常
- **THEN** 下一周期行情正常更新，缓存恢复新鲜

### Requirement: Provider 运行指标

每个 Provider SHALL 维护统一运行指标：request_count、success_count、error_count、timeout_count、last_success_at、last_error_at、last_error、last_duration_ms。error（接口报错/连接失败/解析失败）与 timeout（超过规定时间未完成）SHALL 分开统计。指标 SHALL 由统一封装层（ProviderMetrics）实现，各业务 Provider SHALL NOT 各自实现统计逻辑；指标为进程内存态，重启后重新计数（可接受）。指标变化 SHALL 同步输出结构化日志。

#### Scenario: 正常请求计入成功
- **WHEN** Tencent 请求正常返回
- **THEN** request_count 与 success_count 各增 1，last_duration_ms 更新

#### Scenario: 接口报错计入 error
- **WHEN** Provider 抛出非超时异常（如 HTTP 500、解析失败）
- **THEN** error_count 增 1，last_error_at/last_error 更新

#### Scenario: 超时计入 timeout
- **WHEN** Provider 调用超时
- **THEN** timeout_count 增 1（不计入 error_count），last_error_at 更新

### Requirement: Provider 指标查询接口

`GET /api/admin/status` 的 providers 节 SHALL 按数据源名称返回上述全部指标字段，使开发者可判断哪个 Provider 出问题、最近是否成功、失败多少次、是否超时、最近耗时。

#### Scenario: 查询 Provider 指标
- **WHEN** GET /api/admin/status
- **THEN** providers 含 tencent/akshare/tushare 的 request_count、success_count、error_count、timeout_count、last_duration_ms 等字段

### Requirement: Tushare 全局请求节奏

系统 SHALL 提供进程级 Tushare 请求节奏控制（`TushareRequestGate`，位于 `app/providers/tushare_common.py`）：以线程安全方式（threading.Lock + time.monotonic()）按 endpoint 最小间隔排队全部 Tushare 请求，避免历史同步、估值刷新、交易日历等多个调用方同时触发限流。默认最小间隔 SHALL 为 0.6 秒（约 100 次/分钟），stock_basic SHALL 不低于 1.25 秒/请求，间隔 SHALL 可经配置调整；`TushareFundamentalProvider`、`TushareTradingCalendarProvider`、`TushareHistoricalMarketDataProvider` SHALL 渐进共享同一 gate 与 client factory，接入过程 SHALL NOT 改变旧 Provider 的业务契约与现有页面行为。第一阶段 SHALL NOT 引入高并发 Tushare 请求。

#### Scenario: 多调用方共享限速

- **WHEN** 历史 job、估值 job 与日历 Provider 在相近时刻各自发起 Tushare 请求
- **THEN** 请求经同一 RequestGate 按 endpoint 最小间隔串行发出，无并发突发

#### Scenario: 限流错误可重试

- **WHEN** Tushare 返回限流错误
- **THEN** 调用方按重试策略处理（历史同步计入该日重试次数），RequestGate 不吞掉异常

#### Scenario: 节奏可配置

- **WHEN** 配置调整 request_min_interval_seconds
- **THEN** 后续请求按新间隔排队，无需重启数据获取逻辑重构

### Requirement: 超时归属（单请求而非方法级）

`call_with_metrics` SHALL NOT 对历史 Provider 方法施加固定 wall-clock 超时：一个方法可能包含多个受请求节奏限制的远端请求（`stock_basic` 15 个分片、按证券逐只补齐上千次），方法级上限会把正常节流误判为超时，并在超时后留下仍在发请求的线程与重试重叠。

单请求网络超时 SHALL 位于共享 Tushare transport / 原生 SDK 请求层，对每个真实 HTTP 请求生效，取值 `config.providers.timeout.tushare`。`TushareRequestGate` SHALL 只负责节流，等待 gate 的时间 SHALL NOT 计入单请求网络超时。真实请求超时 SHALL 归一化为 `TushareTimeoutError`（同时是 `TimeoutError` 子类）并计入 `timeout_count`，SHALL NOT 退化为 `error_count`。方法级调用 SHALL 只记录 success/error/duration。

#### Scenario: 多请求复合方法不因节流被误判超时

- **WHEN** `stock_basic` 15 个分片在 1.25 秒/请求的节奏下耗时超过单请求超时时间
- **THEN** 方法正常完成、计入 success_count，不产生 timeout_count

#### Scenario: 真实请求超时仍计入 timeout_count

- **WHEN** 单个 Tushare SDK 请求超过 `providers.timeout.tushare` 未返回
- **THEN** 抛 `TushareTimeoutError`，timeout_count 增 1，error_count 不变，错误文本不含 Token

#### Scenario: 超时后不残留后台请求

- **WHEN** 任一请求超时并进入重试
- **THEN** 上一次请求的线程已随调用返回而结束，SHALL NOT 有被放弃的线程继续发请求与重试重叠

