## ADDED Requirements

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
