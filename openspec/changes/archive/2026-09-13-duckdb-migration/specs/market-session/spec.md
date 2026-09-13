## ADDED Requirements

### Requirement: TradingCalendarProvider 接口

系统 SHALL 定义 `is_trading_day(market, date) -> bool` 接口，分别支持 CN 与 HK；判定结果 SHALL 缓存到 DuckDB（trading_calendar 表），禁止每 60 秒请求日历数据源。

#### Scenario: 非交易日
- **WHEN** 查询周末或节假日
- **THEN** 返回 False（基于交易日历，而非仅判断周一至周五）

#### Scenario: 缓存命中
- **WHEN** 同一日期重复查询
- **THEN** 直接读 DuckDB 缓存

### Requirement: 市场状态判定

MarketSessionService SHALL 根据交易日历与当前北京时间（Asia/Shanghai）返回状态：OPEN / LUNCH_BREAK / CLOSED / HOLIDAY，并提供 `should_refresh(market) -> bool`（仅 OPEN 为 True）。交易时段：CN 09:30-11:30、13:00-15:00；HK 09:30-12:00、13:00-16:00。所有 datetime SHALL 带时区。

#### Scenario: A股上午交易
- **WHEN** 北京时间交易日 10:00
- **THEN** CN 状态为 OPEN

#### Scenario: A股午间休市
- **WHEN** 北京时间交易日 12:00
- **THEN** CN 状态为 LUNCH_BREAK

#### Scenario: A股下午交易
- **WHEN** 北京时间交易日 14:00
- **THEN** CN 状态为 OPEN

#### Scenario: A股收盘
- **WHEN** 北京时间交易日 15:01
- **THEN** CN 状态为 CLOSED

#### Scenario: 港股午休时段
- **WHEN** 北京时间交易日 12:30
- **THEN** HK 状态为 LUNCH_BREAK，CN 同时刻亦为午休或按各自规则独立判定

#### Scenario: 港股收盘
- **WHEN** 北京时间交易日 16:01
- **THEN** HK 状态为 CLOSED

#### Scenario: 节假日
- **WHEN** 当日为该市场非交易日
- **THEN** 该市场状态为 HOLIDAY

### Requirement: 交易日历日期的时区推导

系统 SHALL 使用市场本地时区（Asia/Shanghai，CN 与 HK 均为 UTC+8 无夏令时）推导查询交易日历所用的日期，SHALL NOT 使用服务器本地时区、UTC 或容器默认时区的日期；所有 datetime SHALL 带时区信息。

#### Scenario: 容器为 UTC 时区
- **WHEN** 系统时区为 UTC，当前 UTC 时间为 `2026-08-18T02:00:00+00:00`（北京时间 2026-08-18 10:00，UTC 日期仍为 08-18）
- **THEN** 交易日历查询日期为北京日期 2026-08-18，市场状态按北京 10:00 判定为 OPEN

#### Scenario: 北京时间跨日边界
- **WHEN** 系统时区为 UTC，当前 UTC 时间为 `2026-08-18T17:00:00+00:00`（北京时间 2026-08-19 01:00，UTC 日期为 08-18）
- **THEN** 交易日历查询日期为北京日期 2026-08-19，而非 UTC 日期 2026-08-18

#### Scenario: 状态判定不随系统时区漂移
- **WHEN** 分别在系统时区为 UTC 与 Asia/Shanghai 的环境下注入同一时刻
- **THEN** 两个市场状态判定结果一致

### Requirement: A股港股独立判断

A股与港股 SHALL 各自独立判断状态；一个市场休市 SHALL NOT 阻止另一 OPEN 市场刷新行情。

#### Scenario: A股收盘港股仍交易
- **WHEN** 北京时间 15:30，A股 CLOSED、港股 OPEN
- **THEN** 仅刷新港股相关标的

### Requirement: 状态判定收敛

交易时段与状态判定逻辑 SHALL 仅存在于 MarketSessionService，SHALL NOT 散落在调度器、API 或前端代码。

#### Scenario: 单一判定点
- **WHEN** 后台任务与 API 都需要市场状态
- **THEN** 均调用 MarketSessionService 获取
