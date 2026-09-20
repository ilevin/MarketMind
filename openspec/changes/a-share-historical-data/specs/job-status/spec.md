## MODIFIED Requirements

### Requirement: 后台 Job 接入

QuoteRefreshJob、FundamentalRefreshJob 与 HistorySyncJob SHALL 分别以 job_name `quote_refresh`、`fundamental_refresh`、`history_sync` 接入 JobStatusService，记录每个任务周期的开始/成功/失败。手动刷新接口 SHALL NOT 写入 job_status。JobStatusService 对 HistorySyncJob 仅负责高层健康信息（最近开始、最近成功、最近失败、耗时、连续失败次数）；历史数据业务进度（水位、当前日期、重试、执行记录）SHALL 由 history_sync_state / history_sync_run / history_sync_run_dataset 表承载，SHALL NOT 挤入 job_status。后续新增 Job SHALL 复用同一机制。

#### Scenario: 行情任务可查最近成功时间

- **WHEN** QuoteRefreshJob 完成若干周期后查询状态
- **THEN** quote_refresh 的 last_success_at 为最近一次正常完成时间

#### Scenario: 估值任务可查最近成功时间

- **WHEN** FundamentalRefreshJob 完成刷新后查询状态
- **THEN** fundamental_refresh 的 last_success_at 有值

#### Scenario: 历史任务接入 JobStatus

- **WHEN** HistorySyncJob 完成一次同步（含 PARTIAL 结果）
- **THEN** history_sync 在 job_status 有 last_started_at/last_duration_ms 等高层字段，而水位与执行明细在 history_sync_* 表中查询

#### Scenario: 部分成功不算系统级失败

- **WHEN** 某次历史同步因单数据集失败结束为 PARTIAL
- **THEN** job_status 记录本次结束状态与耗时，具体失败数据集与错误码在 history_sync_run_dataset 中追溯
