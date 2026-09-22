# job-status Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: job_status 数据表

系统 SHALL 提供 `job_status` 表持久化后台任务最近运行状态，以 `job_name` 为主键（每 Job 一行），字段至少包含：last_started_at、last_success_at、last_error_at、last_error、last_duration_ms、consecutive_failures、updated_at。时间戳 SHALL 以 aware 值存储（TIMESTAMPTZ，落库不丢时区、读出即 aware）。状态 SHALL 跨重启保留。

#### Scenario: 重启后状态保留
- **WHEN** Job 成功运行后重启服务再查询状态
- **THEN** last_success_at 等字段仍为重启前的值

### Requirement: Job 状态更新语义

Job 开始时 SHALL 记录 last_started_at；正常完成 SHALL 记录 last_success_at、last_duration_ms 并将 consecutive_failures 清零；异常 SHALL 记录 last_error_at、last_error、last_duration_ms 并使 consecutive_failures 递增。失败 SHALL NOT 清除既有 last_success_at。状态更新 SHALL 经统一的 JobStatusService 封装；状态写入自身失败 SHALL NOT 中断 Job 主流程（仅记日志）。

#### Scenario: 首次成功
- **WHEN** Job 首次运行成功
- **THEN** last_success_at 与 last_duration_ms 有值，consecutive_failures 为 0

#### Scenario: 失败不清除最近成功
- **WHEN** 10:00 成功、10:01 失败
- **THEN** last_success_at 仍为 10:00，last_error_at 为 10:01，consecutive_failures 为 1

#### Scenario: 连续失败后重新成功
- **WHEN** 连续失败 3 次后运行成功
- **THEN** consecutive_failures 归零，last_success_at 更新为本次时间

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

### Requirement: 运行状态查询接口

系统 SHALL 提供 `GET /api/admin/status`，返回 version（当前应用版本）、jobs（各 Job 上述状态字段）与 providers（见 provider-metrics 能力）三组数据；未运行过的 Job/Provider 字段 SHALL 返回 null 而非缺键；时间 SHALL 返回北京时间带时区 ISO 格式。

#### Scenario: 查询后台任务状态
- **WHEN** GET /api/admin/status
- **THEN** 返回 200，顶层含 version，jobs 含 quote_refresh 与 fundamental_refresh 的 last_success_at、last_duration_ms、consecutive_failures 等字段

