## ADDED Requirements

### Requirement: 按股票区间拉取

`HistoricalMarketDataProvider` Protocol 与 `TushareHistoricalMarketDataProvider` SHALL 新增 `get_history_by_stock(dataset, instrument, start_date, end_date) -> ProviderBatch[DailyBar | AdjFactor | DailyBasic | MoneyFlow]`：单股一次请求覆盖完整区间，SHALL NOT 逐日拆分请求。实现 SHALL 映射 Tushare `pro.{daily,adj_factor,daily_basic,moneyflow}(ts_code=…, start_date=…, end_date=…, fields=…)`；`ts_code` SHALL 由传入 Instrument 的主档当前代码构造，SHALL NOT 按代码首位推断交易所。方法 SHALL 复用既有调用层级（Registry 的 `call_with_metrics` → Provider → `TushareRequestGate` → SDK，metrics 键复用 `tushare_history_{dataset}`），共享进程级请求节奏与单请求超时；显式 fields 声明、`raw_row_count` 口径（别名规范化之前的上游行数）与既有别名规范化（含 ALIAS_CONFLICT 检测）全部适用。单次返回行数达到接口行数上限时 SHALL 置 `truncation_risk=True`（单股 16 年约 4000 行正常不触发，作为异常放大返回的防护）。Provider SHALL 接收 Service 传入的 Instrument 快照，SHALL NOT 自行访问数据库。既有按 trade_date 的全市场方法与截断 fallback SHALL 保留（Protocol 兼容），但同步编排 SHALL NOT 再调用它们于日级数据集。

#### Scenario: 区间一次请求

- **WHEN** 执行器需要补齐某股 2010-01-04 至 2026-09-18 的 daily
- **THEN** Provider 经 gate 节流发起一次 `pro.daily(ts_code=…, start_date=20100104, end_date=20260918, fields=…)` 请求，返回区间全部行

#### Scenario: 主档代码构造 ts_code

- **WHEN** 传入 Instrument 的主档规范 ts_code 为 `001872.SZ`
- **THEN** 请求使用 `001872.SZ`，即使该股历史数据曾以 `000022.SZ` 返回，响应经别名规范化后仍映射回同一 instrument_id

#### Scenario: 行数异常放大拒绝提交

- **WHEN** 单股区间请求返回恰好达到接口行数上限的行数
- **THEN** ProviderBatch.truncation_risk=True，该股该次尝试校验失败、水位不推进

#### Scenario: 既有按日方法保留

- **WHEN** 检查 Provider 类与 Protocol 定义
- **THEN** 按 trade_date 的既有方法与截断 fallback 仍存在且行为不变，仅同步编排不再调用

#### Scenario: 指标进入现有体系

- **WHEN** 个股区间请求发生
- **THEN** ProviderMetricsRegistry 对应 `tushare_history_{dataset}` key 的 request/success/error/timeout 计数更新，无第二套统计实现

## MODIFIED Requirements

### Requirement: 历史 ts_code 别名规范化

Provider SHALL 在把 Tushare 历史事实的 `ts_code` 映射为 `instrument_id` 之前，把**已登记**的历史证券代码改写为该证券当前的规范代码（登记表 `TUSHARE_TS_CODE_ALIASES`，首条 `000022.SZ -> 001872.SZ`：深赤湾A 于 2018-12-26 变更为招商港口）。规范化 SHALL 只作用于四个日级事实数据集（daily / adj_factor / daily_basic / moneyflow），SHALL NOT 作用于 `stock_basic` / `stock_company` / `namechange`——主档是 `instrument` 的来源，改写主档会造出第二条证券记录。

Provider SHALL NOT 按 `.SZ` / `.SH` / `.BJ` 后缀、代码段或名称相似度推断别名，SHALL NOT 因为某代码不在主档就放行；未登记的未知 `ts_code` SHALL 仍按 `UNKNOWN_INSTRUMENT` 拒绝该次提交、不推进水位。

新旧代码并存检测 SHALL 以 `(canonical_ts_code, trade_date)` 为分组键，统一适用于单日批次与区间批次（同一交易日同一证券至多一行）；区间批次中同一证券多个不同交易日各一行 SHALL NOT 被误判为并存。同一分组内新旧代码并存且业务字段一致时只交付规范代码行、记录 `action=drop_legacy` WARNING；至少一个业务字段不同时抛 `ALIAS_CONFLICT`。

`ProviderBatch.raw_row_count` SHALL 记录规范化**之前**的上游原始行数，SHALL NOT 因合并而"美化"监控口径。

#### Scenario: 仅返回旧代码

- **WHEN** 某区间 daily 只返回 `ts_code=000022.SZ`，主档只有 `CN:STOCK:001872`
- **THEN** 该行 `ts_code` 被改写为 `001872.SZ`、映射为 `CN:STOCK:001872`，该股该次同步正常提交、水位推进，SHALL NOT 抛 `UNKNOWN_INSTRUMENT`，SHALL NOT 创建 `CN:STOCK:000022` 证券

#### Scenario: 区间批次同股多日不误判

- **WHEN** 某股 16 年区间请求返回同一 canonical_ts_code 的约 4000 个不同交易日各一行
- **THEN** 全部行正常交付，不触发并存检测，不抛 ALIAS_CONFLICT

#### Scenario: 区间内某日新旧并存且字段一致

- **WHEN** 区间批次中同一交易日同时返回 `000022.SZ` 与 `001872.SZ`，且两者业务字段一致
- **THEN** Provider 只交付一行（规范代码 `001872.SZ`），`raw_row_count` 仍计入两行，并记录含 `action=drop_legacy` 的 WARNING 日志

#### Scenario: 区间内某日新旧并存但字段冲突

- **WHEN** 区间批次中同一交易日同时返回 `000022.SZ` 与 `001872.SZ`，且至少一个业务字段取值不同
- **THEN** Provider 抛 `ALIAS_CONFLICT`（TushareError 子类），该股该次尝试失败、水位 SHALL NOT 推进，异常文本含 endpoint、交易日与两个 ts_code 以及不一致字段

#### Scenario: 未登记的未知代码仍被拒绝

- **WHEN** 事实数据返回主档中不存在的 `ts_code=999999.SZ` 且该代码未登记为别名
- **THEN** 仍按 `UNKNOWN_INSTRUMENT` 拒绝该次提交，不创建占位证券，水位不动

#### Scenario: 两条抓取路径行为一致

- **WHEN** 主路径命中行数上限走逐证券 fallback，且上游对该证券的历史日期仍返回旧代码
- **THEN** fallback 与主路径使用同一规范化口径，产出的 `instrument_id` 与规范 `ts_code` 与主路径完全一致

#### Scenario: 主档数据集不参与别名改写

- **WHEN** `stock_basic` 的分片返回 `ts_code=000022.SZ`
- **THEN** 该记录原样交付（`instrument_id=CN:STOCK:000022`），由 Service 按 `list_status` 处置，Provider SHALL NOT 在此改写为主档中的其它代码
