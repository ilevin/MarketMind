## MODIFIED Requirements

### Requirement: HistoryProviderRegistry 选源

系统 SHALL 提供 `HistoryProviderRegistry`（模式与现有 QuoteProviderRegistry 一致）：构造 Provider 单例、经 `call_with_metrics` 包装调用、按源注入超时；选源 SHALL 按数据集进行——股票 8 个数据集（stock_basic/trade_cal/namechange/stock_company/daily/adj_factor/daily_basic/moneyflow）SHALL 从 `config.providers.history.market_data` 选择源（默认 tushare，语义与既有单源行为一致）；`etf_daily` SHALL 从 `config.providers.history.etf_daily` 选择源（默认 eastmoney）；`etf_adj_factor` SHALL 从 `config.providers.history.etf_adj_factor` 选择源（默认 tushare）；`etf_basic`（ETF universe 刷新）SHALL 复用 `providers.history.etf_daily` 指定的源（universe 与日线同源，不单设选源键）。超时 SHALL 按源注入：tushare 源取 `config.providers.timeout.tushare`，eastmoney 源取 `config.providers.timeout.akshare`。HistorySyncService SHALL 经 Registry 获取数据，SHALL NOT 直接实例化具体 Provider 或 import tushare/akshare。metrics source key SHALL 使用 `{source}_history_{dataset}` 规则：tushare 源沿用 `tushare_history_stock_basic` / `tushare_history_daily` / `tushare_history_adj_factor` / `tushare_history_daily_basic` / `tushare_history_moneyflow` / `tushare_history_stock_company` / `tushare_history_namechange`，ETF 数据集为 `eastmoney_history_etf_daily` / `tushare_history_etf_adj_factor`（交易日历不在此列——trade_cal 由现有 TushareTradingCalendarProvider 扩展提供，不新增第二个 trade_cal Provider），复用现有 `ProviderMetricsRegistry`，SHALL NOT 新建历史专属 metrics/timeout 体系。

#### Scenario: 配置选源

- **WHEN** config.yaml 配置 providers.history.market_data 选择 tushare
- **THEN** Registry 构造 TushareHistoricalMarketDataProvider 单例，Service 经 Registry 调用

#### Scenario: 指标进入现有体系

- **WHEN** 历史数据集请求发生
- **THEN** ProviderMetricsRegistry 中对应 tushare_history_* key 的 request/success/error/timeout 计数更新，无第二套统计实现

#### Scenario: 按数据集路由

- **WHEN** Service 经 Registry 请求 etf_daily 与 etf_adj_factor 各一次
- **THEN** etf_daily 请求由 providers.history.etf_daily 指定的源发出、etf_adj_factor 请求由 providers.history.etf_adj_factor 指定的源发出，互不影响

#### Scenario: universe 与日线同源

- **WHEN** etf_basic universe 刷新发生
- **THEN** 该请求经 providers.history.etf_daily 指定的源（默认 eastmoney）发出，不单设 universe 选源键

#### Scenario: 股票数据集行为不变

- **WHEN** Registry 改造后股票 8 个数据集请求发生
- **THEN** 全部仍路由到 providers.history.market_data 指定的源（默认 tushare），调用方式、metrics 键与限流行为与改造前一致

#### Scenario: ETF 指标进入现有体系

- **WHEN** ETF 数据集请求发生
- **THEN** ProviderMetricsRegistry 中 eastmoney_history_etf_daily / tushare_history_etf_adj_factor 的 request/success/error/timeout 计数更新，无第二套统计实现

#### Scenario: 数据源切换只改配置

- **WHEN** 更换 etf_daily 或 etf_adj_factor 的选源配置并存在对应 Provider 实现
- **THEN** 上层业务代码零改动完成切换，Service 不感知具体源

### Requirement: 历史 ts_code 别名规范化

Provider SHALL 在把 Tushare 历史事实的 `ts_code` 映射为 `instrument_id` 之前，把**已登记**的历史证券代码改写为该证券当前的规范代码（登记表 `TUSHARE_TS_CODE_ALIASES`，首条 `000022.SZ -> 001872.SZ`：深赤湾A 于 2018-12-26 变更为招商港口）。规范化 SHALL 只作用于五个日级事实数据集（daily / adj_factor / daily_basic / moneyflow / etf_adj_factor），SHALL NOT 作用于 `stock_basic` / `stock_company` / `namechange` / `etf_basic`——主档是 `instrument` 的来源，改写主档会造出第二条证券记录。`etf_daily`（东方财富源按 symbol 请求与返回）SHALL NOT 经过 Tushare 别名层。ETF 数据集的别名登记初始为空（ETF 无代码变更先例），机制与登记表复用自股票侧，未来 ETF 代码变更可登记。

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

#### Scenario: ETF 零登记不改写

- **WHEN** fund_adj 返回某 ETF 的 ts_code 且 ETF 别名登记为空
- **THEN** 该 ts_code 原样映射主档（`cn_etf_basic`），不发生改写也不误报别名冲突；未知 ETF 代码仍按 UNKNOWN_INSTRUMENT 拒绝

## ADDED Requirements

### Requirement: ETF 数据源接入与内部标准模型

系统 SHALL 在既有 Provider 体系内接入 ETF 数据源，SHALL NOT 新建第二套 Provider 基础设施：内部标准模型 SHALL 在 `app/providers/base.py` 新增 `EtfDailyBar`（frozen dataclass：instrument_id、ts_code、trade_date、open、high、low、close、volume、amount、turnover_rate）与 `EtfUniverseRecord`（symbol、name、exchange、list_date 可空），复权因子 SHALL 复用现有 `AdjFactor` 模型（字段同构，SHALL NOT 为 ETF 复制第二份）；AKShare/东方财富 DataFrame 与第三方原始字段名 SHALL NOT 越过具体 Provider 边界进入 Service/Repository。既有 `HistoricalMarketDataProvider` Protocol 的 `get_history_by_stock(dataset, instrument, start_date, end_date)` 签名 SHALL 扩展覆盖 ETF 数据集：dataset 取值集合扩展 `etf_daily`/`etf_adj_factor`，返回类型联合扩展为 `ProviderBatch[DailyBar | AdjFactor | DailyBasic | MoneyFlow | EtfDailyBar]`（etf_adj_factor 复用 AdjFactor）；并新增 `get_etf_universe()` 方法声明。

ETF 日线 SHALL 由 `EastmoneyEtfHistoryProvider`（AKShare 适配东方财富历史接口）提供：单只 ETF 一次区间请求覆盖完整缺失区间（SHALL NOT 逐日拆分），请求 SHALL 使用不复权口径（保存原始 OHLC，符合"不存储派生复权数据"原则）；实现 SHALL 延迟 import akshare（沿用 `AkshareQuoteProvider` 模式）；显式字段清洗沿用 `safe_float` 口径（脏值转 None，NULL 原样保留）；单次返回行数达到行数上限阈值 SHALL 置 truncation_risk=True（异常放大返回防护，单 ETF 十六年约 4000 行正常不触发）；Provider SHALL 接收 Service 传入的 Instrument 快照、SHALL NOT 自行访问数据库。ETF universe SHALL 经同体系暴露 `get_etf_universe()` 能力（一次请求返回当前上市 ETF 全集）。

ETF 复权因子 SHALL 由现有 `TushareHistoricalMarketDataProvider` 扩展支持 `etf_adj_factor` 数据集：映射 `pro.fund_adj(ts_code=…, start_date=…, end_date=…, fields=…)`，显式 fields 声明、`raw_row_count` 口径沿用；etf_adj_factor 纳入 Tushare 区间数据集映射后 SHALL 自动流经既有 ts_code 别名规范化（见「历史 ts_code 别名规范化」——初始零 ETF 登记项等价不改写，未登记未知代码按 UNKNOWN_INSTRUMENT 拒绝）；SHALL 复用 `TushareTransport`/`TushareRequestGate`/超时/`classify_tushare_exception` 全部既有基建，SHALL NOT 自建 client 或绕过 gate；ts_code SHALL 由 `cn_etf_basic` 主档当前代码构造。fund_adj 的行数上限、行覆盖语义（每交易日一行 vs 仅除权事件日一行）与停牌/退市空结果 SHALL 经在线实测确认并固化进离线 fake 基线，行覆盖语义差异 SHALL 只允许影响 Quant 层 `factor_at` 单点的取值方式（见 etf-quant-api 能力）。

#### Scenario: AKShare DataFrame 不越界

- **WHEN** 检查 EastmoneyEtfHistoryProvider 的调用方（services/、repositories/）
- **THEN** 不出现 AKShare/东方财富原始类型或字段名，ETF 数据以 EtfDailyBar 内部标准模型传递

#### Scenario: 日线区间一次请求不复权

- **WHEN** 执行器需要补齐某 ETF 2010-01-04 至 2026-09-30 的日线
- **THEN** Provider 发起一次不复权口径的区间请求，返回区间全部行并清洗为 EtfDailyBar

#### Scenario: fund_adj 复用既有基建

- **WHEN** etf_adj_factor 单券区间请求发生
- **THEN** 请求经共享 TushareRequestGate 按最小间隔串行发出、复用超时与异常分类，metrics 计入 `tushare_history_etf_adj_factor`

#### Scenario: 行数异常放大拒绝提交

- **WHEN** 单只 ETF 区间请求返回达到行数上限阈值的行数
- **THEN** ProviderBatch.truncation_risk=True，该券该次尝试校验失败、水位不推进

#### Scenario: 未知 ETF 代码拒绝

- **WHEN** fund_adj 返回主档中不存在的 ts_code 且未登记为别名
- **THEN** 按 UNKNOWN_INSTRUMENT 拒绝该次提交，不创建占位证券，水位不动

### Requirement: 东财请求节奏与异常归一化

东方财富/AKShare 请求 SHALL 经新建的进程级 `EastmoneyRequestGate`（模式对齐 `TushareRequestGate`：按最小间隔串行放行，间隔 `history.etf_request_min_interval_seconds` 默认 0.5 秒）发出，SHALL NOT 与 Tushare gate 混用（不同上游、不同限流模型）。东财异常 SHALL 归一化为带 error_code 的 `EastmoneyProviderError` 体系：EASTMONEY_TIMEOUT（可重试，同时为 TimeoutError 子类）、EASTMONEY_API_ERROR（可重试）、SCHEMA_MISMATCH 与 UNKNOWN_INSTRUMENT（配置类错误，经既有 `CONFIG_ERROR_CODES` 判定单券首试即终态失败——`is_config_error` 按错误码判断，两个错误码已在集合中，无需为东财新增错误码）；错误文本 SHALL NOT 包含敏感信息。超时 SHALL 取 `providers.timeout.akshare` 既有配置。

#### Scenario: 连续请求被节流

- **WHEN** ETF 全量回填期间多只 ETF 连续请求东财接口
- **THEN** 相邻请求间隔不小于配置的最小间隔，不并发冲击上游

#### Scenario: 超时可重试

- **WHEN** 某次东财请求超时抛出异常
- **THEN** 归一化为 EASTMONEY_TIMEOUT 进入正常重试路径，重试耗尽该券 task=failed

#### Scenario: 响应结构异常快速失败

- **WHEN** 东财接口返回结构无法解析（必要字段缺失）
- **THEN** 归一化为 SCHEMA_MISMATCH 配置类错误，该券首次尝试即终态失败，不睡满退避轮次
