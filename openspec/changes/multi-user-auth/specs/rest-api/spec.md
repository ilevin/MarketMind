# rest-api Specification(Delta)

## MODIFIED Requirements

### Requirement: 行情查询 API

`GET /api/quotes` SHALL 要求登录,返回当前登录用户自选股票/ETF 行情:顶层含 market_status(CN/HK 各自状态),items 含 instrument_id、symbol、name、market、asset_type、price、change_percent、volume_ratio、pe_ttm、pb、dividend_yield_ttm、quote_source、fundamental_source、source_timestamp、is_stale、tags(`[{id, name}]` 数组,空数组表示无标签)。缺失字段 SHALL 返回 null,SHALL NOT 返回 "-"(由前端渲染)。

API SHALL 支持标签筛选查询参数:`tag_id=<id>` 仅返回关联(含)该标签的条目;`untagged=true` 仅返回无任何标签的条目;两参数互斥(同传返回 422);无参数返回全部。筛选 SHALL 仅作用于返回层,SHALL NOT 触发任何 Provider 请求或影响行情刷新。

#### Scenario: 正常返回
- **WHEN** 登录用户 GET /api/quotes 且缓存有数据
- **THEN** 返回 200 与 items 列表,估值缺失字段为 null,每条目含 tags 数组(无标签为空数组)

#### Scenario: 合并估值
- **WHEN** A股股票有 fundamental_snapshot
- **THEN** pe_ttm/pb/dividend_yield_ttm 来自估值快照,fundamental_source 为 tushare

#### Scenario: 按标签筛选
- **WHEN** GET /api/quotes?tag_id=3(条目 A 关联 [3],条目 B 关联 [3, 5])
- **THEN** 仅返回条目 A 与 B(均含标签 3),其余字段行为不变

#### Scenario: 无标签筛选
- **WHEN** GET /api/quotes?untagged=true
- **THEN** 仅返回无任何标签的条目

#### Scenario: 筛选参数互斥
- **WHEN** GET /api/quotes?tag_id=3&untagged=true
- **THEN** 返回 422 校验错误

#### Scenario: 未登录返回 401
- **WHEN** 未携带有效 Session Cookie 请求 GET /api/quotes
- **THEN** 返回 401 Unauthorized

### Requirement: 指数查询 API

`GET /api/indices` SHALL 要求登录,返回当前登录用户 index_watchlist 配置的指数行情,items 不含 PE/PB/股息率字段。

#### Scenario: 指数返回
- **WHEN** 登录用户 GET /api/indices
- **THEN** items 仅含名称、点位、涨跌幅、来源、时间、is_stale 等行情字段

#### Scenario: 指数配置按用户区分
- **WHEN** 用户 A 配置了上证指数、用户 B 未配置任何指数
- **THEN** B 请求 GET /api/indices 返回空 items,A 返回自己的指数

### Requirement: watchlist 与 index-watchlist API

系统 SHALL 提供 17.3-17.10 节全部端点(GET/POST/DELETE/PUT order),全部要求登录且自动以当前登录用户为数据作用域,SHALL NOT 通过查询参数或请求体接受归属 user_id。状态码:201 添加成功、409 当前用户重复、404 不存在、204 删除成功。

#### Scenario: 添加已存在
- **WHEN** POST /api/watchlist 重复标的(当前用户已持有)
- **THEN** 409 Conflict

#### Scenario: 不同用户添加同一标的
- **WHEN** 用户 A 已持有 600519,用户 B POST 同一标的
- **THEN** 返回 201

### Requirement: 管理与健康接口

系统 SHALL 提供 POST /api/admin/refresh/quotes、POST /api/admin/refresh/fundamentals、GET /api/admin/status 与 GET /health。`/api/admin/*` 全部 SHALL 要求 admin 角色(在 Router 层统一声明依赖):未登录返回 401,普通用户返回 403。`/health` SHALL 保持匿名可访问,健康检查 SHALL 只检查应用与数据库,SHALL NOT 实时调用 AKShare/Tushare。`/health` 响应 SHALL 含 version 字段(当前应用版本)。`/api/admin/status` SHALL 返回 version(当前应用版本)、后台 Job 运行状态(jobs)与 Provider 运行指标(providers)。

#### Scenario: 健康检查
- **WHEN** 匿名 GET /health 且数据库可连接
- **THEN** 返回 `{"status":"ok","database":"ok","version":"v0.1.0"}`

#### Scenario: 查询运行状态
- **WHEN** admin 请求 GET /api/admin/status
- **THEN** 返回 200,顶层含 version,jobs(quote_refresh/fundamental_refresh)与 providers(tencent/akshare/tushare)状态数据

#### Scenario: 普通用户访问管理接口被拒
- **WHEN** role=user 请求 POST /api/admin/refresh/quotes
- **THEN** 返回 403,不触发刷新

#### Scenario: 未登录访问管理接口
- **WHEN** 匿名请求 GET /api/admin/status
- **THEN** 返回 401

## ADDED Requirements

### Requirement: 认证 API 端点

系统 SHALL 提供 `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`、`POST /api/auth/change-password`(详见 user-authentication 规格)。

#### Scenario: 查询当前用户
- **WHEN** 登录用户 GET /api/auth/me
- **THEN** 返回 200 与 username、role
