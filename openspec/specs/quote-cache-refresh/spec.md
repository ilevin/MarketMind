# quote-cache-refresh Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 双层缓存

系统 SHALL 采用「后台任务 -> Provider -> 内存缓存 -> DuckDB quote_snapshot」链路；API（/api/quotes、/api/indices）SHALL 只读内存缓存，内存缺失时回退 DuckDB 最近一条。浏览器 SHALL NOT 直接触发数据源请求。缓存中 SHALL 保存 source_timestamp（行情源时间）与 fetched_at（服务器请求时间）两个不同概念的字段。quote_snapshot 每只证券 SHALL 至多一行（instrument_id 为主键），行情更新 SHALL 为原地 upsert。

#### Scenario: 首次打开页面
- **WHEN** 任意时刻首次打开首页
- **THEN** 读取后端缓存；收盘后仍能看到最后一次成功行情

#### Scenario: 数据源故障回退
- **WHEN** AKShare 全部失败且内存无数据
- **THEN** API 返回 DuckDB 最近快照，HTTP 200，不返回 500

#### Scenario: 失败不清缓存
- **WHEN** 某轮刷新 Provider 抛异常
- **THEN** 内存缓存与 DuckDB 快照保留上次成功数据

### Requirement: 60 秒后台刷新

后台任务 SHALL 默认每 60 秒执行一轮：读取 watchlist + index_watchlist -> 按市场分组 -> 判断市场状态 -> 仅刷新 OPEN 市场 -> 按资产类型调用 QuoteProvider -> 更新内存缓存 -> 保存 QuoteSnapshot。

#### Scenario: 交易时段刷新
- **WHEN** 市场 OPEN
- **THEN** 每轮执行行情刷新并更新缓存与快照

#### Scenario: 午休不刷新
- **WHEN** 市场 LUNCH_BREAK
- **THEN** 该市场不执行常规行情请求

#### Scenario: 收盘后不刷新
- **WHEN** 市场 CLOSED
- **THEN** 该市场不执行常规行情请求

#### Scenario: 节假日不刷新
- **WHEN** 市场 HOLIDAY
- **THEN** 该市场不执行行情请求

### Requirement: 收盘补抓

当市场状态发生 OPEN -> CLOSED 切换时，系统 SHALL 额外执行一次该市场收盘行情刷新，然后停止常规刷新。

#### Scenario: 收盘边沿
- **WHEN** 上一轮检测 CN 为 OPEN，本轮检测为 CLOSED
- **THEN** 补抓一次 A股行情并保存，本轮之后停止 A股常规刷新

### Requirement: stale 判定

系统 SHALL 为每条行情计算 is_stale：市场 OPEN 且超过 stale_seconds（默认 180 秒）未取得新行情时为 stale；市场处于 LUNCH_BREAK/CLOSED/HOLIDAY 时 SHALL NOT 因时间流逝将最后有效行情标记为 stale。

#### Scenario: 交易时段过期
- **WHEN** 市场 OPEN 且 fetched_at 距今超过 180 秒
- **THEN** is_stale 为 true

#### Scenario: 收盘后不过期
- **WHEN** 市场 CLOSED 且当晚访问页面
- **THEN** 收盘时最后的行情 is_stale 为 false，页面显示收盘时间

### Requirement: 手动刷新管理接口

系统 SHALL 提供 `POST /api/admin/refresh/quotes` 与 `POST /api/admin/refresh/fundamentals` 用于调试维护；返回 `{success, updated, failed}`；普通首页 SHALL NOT 周期性调用该接口。

#### Scenario: 手动刷新
- **WHEN** POST /api/admin/refresh/quotes
- **THEN** 执行一次刷新并返回成功/失败统计

