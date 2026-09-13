## ADDED Requirements

### Requirement: 行情查询 API

`GET /api/quotes` SHALL 返回自选股票/ETF 行情：顶层含 market_status（CN/HK 各自状态），items 含 instrument_id、symbol、name、market、asset_type、price、change_percent、volume_ratio、pe_ttm、pb、dividend_yield_ttm、quote_source、fundamental_source、source_timestamp、is_stale、tags（`[{id, name}]` 数组，空数组表示无标签）。缺失字段 SHALL 返回 null，SHALL NOT 返回 "-"（由前端渲染）。

API SHALL 支持标签筛选查询参数：`tag_id=<id>` 仅返回关联（含）该标签的条目；`untagged=true` 仅返回无任何标签的条目；两参数互斥（同传返回 422）；无参数返回全部。筛选 SHALL 仅作用于返回层，SHALL NOT 触发任何 Provider 请求或影响行情刷新。

#### Scenario: 正常返回
- **WHEN** GET /api/quotes 且缓存有数据
- **THEN** 返回 200 与 items 列表，估值缺失字段为 null，每条目含 tags 数组（无标签为空数组）

#### Scenario: 合并估值
- **WHEN** A股股票有 fundamental_snapshot
- **THEN** pe_ttm/pb/dividend_yield_ttm 来自估值快照，fundamental_source 为 tushare

#### Scenario: 按标签筛选
- **WHEN** GET /api/quotes?tag_id=3（条目 A 关联 [3]，条目 B 关联 [3, 5]）
- **THEN** 仅返回条目 A 与 B（均含标签 3），其余字段行为不变

#### Scenario: 无标签筛选
- **WHEN** GET /api/quotes?untagged=true
- **THEN** 仅返回无任何标签的条目

#### Scenario: 筛选参数互斥
- **WHEN** GET /api/quotes?tag_id=3&untagged=true
- **THEN** 返回 422 校验错误

### Requirement: 指数查询 API

`GET /api/indices` SHALL 返回 index_watchlist 配置的指数行情，items 不含 PE/PB/股息率字段。

#### Scenario: 指数返回
- **WHEN** GET /api/indices
- **THEN** items 仅含名称、点位、涨跌幅、来源、时间、is_stale 等行情字段

### Requirement: watchlist 与 index-watchlist API

系统 SHALL 提供 17.3-17.10 节全部端点（GET/POST/DELETE/PUT order），状态码：201 添加成功、409 重复、404 不存在、204 删除成功。

#### Scenario: 添加已存在
- **WHEN** POST /api/watchlist 重复标的
- **THEN** 409 Conflict

### Requirement: 管理与健康接口

系统 SHALL 提供 POST /api/admin/refresh/quotes、POST /api/admin/refresh/fundamentals、GET /api/admin/status 与 GET /health。健康检查 SHALL 只检查应用与数据库，SHALL NOT 实时调用 AKShare/Tushare。`/health` 响应 SHALL 含 version 字段（当前应用版本）。`/api/admin/status` SHALL 返回 version（当前应用版本）、后台 Job 运行状态（jobs）与 Provider 运行指标（providers）。

#### Scenario: 健康检查
- **WHEN** GET /health 且数据库可连接
- **THEN** 返回 `{"status":"ok","database":"ok","version":"v0.1.0"}`

#### Scenario: 查询运行状态
- **WHEN** GET /api/admin/status
- **THEN** 返回 200，顶层含 version，jobs（quote_refresh/fundamental_refresh）与 providers（tencent/akshare/tushare）状态数据

### Requirement: 全局容错

任何单个 Provider 失败 SHALL NOT 导致 HTTP 500 或进程崩溃；所有 API SHALL 使用明确的 Pydantic Schema；时间统一返回北京时间（带时区 ISO 格式）。

#### Scenario: 单市场故障
- **WHEN** 港股数据源故障
- **THEN** /api/quotes 返回 200，A股数据正常，港股显示最后成功数据并标记过期
