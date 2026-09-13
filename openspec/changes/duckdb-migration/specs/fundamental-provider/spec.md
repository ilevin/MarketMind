## ADDED Requirements

### Requirement: FundamentalProvider 统一接口

系统 SHALL 定义 FundamentalProvider 接口：`get_fundamentals(instruments) -> dict[instrument_id, Fundamental]`；V1 实现 TushareFundamentalProvider，基于 Tushare `daily_basic` 提供A股股票的 pe_ttm、pb、dividend_yield_ttm。

#### Scenario: A股股票估值
- **WHEN** 查询 CN:STOCK 标的估值
- **THEN** 返回 Fundamental（pe_ttm、pb、dividend_yield_ttm），缺失为 None

#### Scenario: ETF 与指数无估值
- **WHEN** 标的 asset_type 为 ETF 或 INDEX
- **THEN** 不请求、不写入估值数据，页面显示 `-`

### Requirement: 更新频率

估值数据 SHALL 每日收盘后更新一次（每日一次），保存到 fundamental_snapshot（复合主键 (instrument_id, trade_date)）；应用启动时当天自选 A 股估值未全覆盖 SHALL 执行一次更新。周期检查（每 30 分钟）时，若最近交易日存在自选 A 股缺失当日估值，SHALL 补刷一次。「覆盖」指当日行存在且三项指标（PE/PB/股息率）均非空；任一指标为空的行 SHALL 视为未覆盖（应对数据源当日指标延迟生成，指标当日确无值的标的将每 30 分钟重试至当日结束，每次为一次全市场查询）。对当日确认无数据（停牌、新股等，补刷后仍缺失）的标的，SHALL NOT 在当日重复请求（内存标记，进程重启后允许再试一次）；`POST /api/admin/refresh/fundamentals` 手动刷新 SHALL 忽略该标记并重试全部自选 A 股。估值 SHALL NOT 参与每 60 秒行情刷新。

#### Scenario: 每日一次
- **WHEN** 当日自选 A 股估值已全覆盖
- **THEN** 不再重复请求 Tushare

#### Scenario: 盘中新增自选补齐估值
- **WHEN** 当日估值刷新完成后，用户又添加了新的自选 A 股股票
- **THEN** 下一次周期检查发现缺失并补刷当日估值，新股票的 PE/PB/股息率可显示

#### Scenario: 停牌股不反复请求
- **WHEN** 某自选 A 股当日停牌，Tushare 无该日记录，补刷后仍缺失
- **THEN** 当日后续周期检查跳过该标的，不再对其发起请求

#### Scenario: 当日指标延迟生成的空指标行继续补刷
- **WHEN** 某自选 A 股当日快照任一指标（如股息率）为空
- **THEN** 该标的视为未覆盖，继续参与周期补刷，直至指标回填或当日结束

#### Scenario: 手动强制刷新
- **WHEN** 调用 `POST /api/admin/refresh/fundamentals`
- **THEN** 对全部自选 A 股重新请求估值，忽略已尝试标记

### Requirement: Token 读取与降级

Tushare Token SHALL 只从 config.yaml 的 tushare.token 读取，SHALL NOT 依赖环境变量。Token 缺失时应用 SHALL 正常启动，Tushare 功能记录明确配置错误，估值字段降级为空。

#### Scenario: 无 Token 启动
- **WHEN** config.yaml 中 token 为空或缺失
- **THEN** 应用启动成功，日志含配置错误提示，估值列为 `-`，日志不输出 Token

#### Scenario: Tushare 失败
- **WHEN** Tushare 请求异常或限流
- **THEN** 不崩溃，估值保留最后快照或为空，日志记录失败

### Requirement: 证券基础信息与日历

证券基础信息（名称等）SHALL 每天更新一次；A股交易日历按需获取并缓存到 DuckDB 的 trading_calendar 表，SHALL NOT 以 60 秒级频率调用 Tushare 日历接口，也 SHALL NOT 在每次页面访问时调用 Tushare。

#### Scenario: 日历缓存
- **WHEN** 同一交易日历日期被多次查询
- **THEN** 仅首次触发数据源请求，后续命中 DuckDB 缓存

### Requirement: 添加自选后的即时估值获取

添加自选股票成功后，系统 SHALL 立即获取该股最近一期估值（per-stock 查询，不受收盘时间限制）并写入 fundamental_snapshot；仅 CN/STOCK 生效；获取失败 SHALL NOT 影响添加结果。

#### Scenario: 添加 A 股股票后立即显示估值
- **WHEN** 添加 CN/STOCK 自选成功
- **THEN** 该股最近一期 PE/PB/股息率被写入并可显示，无需等待收盘后的周期刷新

#### Scenario: 添加 ETF 或港股不触发估值获取
- **WHEN** 添加 ETF 或港股股票
- **THEN** 不发起估值请求

#### Scenario: 估值获取失败不影响添加
- **WHEN** 添加成功但 Tushare 请求异常
- **THEN** 添加接口仍返回 201，失败仅记录日志
