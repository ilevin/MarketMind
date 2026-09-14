# quote-cache-refresh Specification(Delta)

## MODIFIED Requirements

### Requirement: 60 秒后台刷新

后台任务 SHALL 默认每 60 秒执行一轮:跨用户读取全部 watchlist + index_watchlist 并按 `DISTINCT instrument_id` 去重 -> 按市场分组 -> 判断市场状态 -> 仅刷新 OPEN 市场 -> 按资产类型调用 QuoteProvider -> 更新内存缓存 -> 保存 QuoteSnapshot。多用户关注同一证券时该证券 SHALL 仅刷新一次。

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

#### Scenario: 多用户重复关注去重
- **WHEN** 用户 A、B、C 都关注 600519,市场 OPEN 时执行一轮刷新
- **THEN** 600519 仅产生一次行情请求与一次快照 upsert,而非三次

## ADDED Requirements

### Requirement: 系统作用域自选查询

后台任务(缓存预热、周期刷新、收盘补抓)SHALL 使用系统作用域查询获取全部用户的 `DISTINCT instrument_id` 集合,SHALL NOT 使用用户作用域 Repository;用户删除自选 SHALL NOT 触发全局 instrument 或行情快照数据的删除。

#### Scenario: 用户删除自选不影响刷新集合中他人标的
- **WHEN** 用户 A 删除 600519 且用户 B 仍关注
- **THEN** 下一轮后台刷新仍包含 600519

#### Scenario: 无人关注后快照保留
- **WHEN** 某证券不再被任何用户关注
- **THEN** 系统不主动删除其 instrument 与 quote_snapshot 数据(数据保留策略属未来独立需求)

#### Scenario: 管理员手动刷新全局数据
- **WHEN** admin POST /api/admin/refresh/quotes
- **THEN** 按去重后的全局集合执行刷新,行为与后台刷新一致
