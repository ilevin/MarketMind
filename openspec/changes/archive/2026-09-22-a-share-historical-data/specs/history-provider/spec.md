## ADDED Requirements

### Requirement: HistoricalMarketDataProvider 协议与内部标准模型

系统 SHALL 在现有 `app/providers/base.py` 中定义历史数据内部标准模型（StockBasicRecord、StockCompanyRecord、StockNameChangeRecord、DailyBar、AdjFactor、DailyBasic、MoneyFlow）与 `HistoricalMarketDataProvider` Protocol，并定义 `ProviderBatch[T]`（records、source、raw_row_count、truncation_risk=False）。Tushare DataFrame、SDK 对象与第三方原始字段名 SHALL NOT 越过具体 Provider 边界进入 Service/Repository。SHALL NOT 新建 `app/providers/history/base.py` 作为第二套 Provider 基础设施。

#### Scenario: Service 侧无 Tushare 类型

- **WHEN** 检查 services/ 与 repositories/ 代码
- **THEN** 不出现 tushare DataFrame/SDK 类型或 Tushare 原始字段名，历史数据以内部标准模型传递

#### Scenario: ProviderBatch 携带采集元信息

- **WHEN** Provider 完成一次获取
- **THEN** 返回 ProviderBatch，含记录列表、source、raw_row_count 与 truncation_risk 标志

### Requirement: HistoryProviderRegistry 选源

系统 SHALL 新增 `HistoryProviderRegistry`（模式与现有 QuoteProviderRegistry 一致）：从 `config.providers.history` 选择 Provider、构造单例、经 `call_with_metrics` 包装调用、注入 `config.providers.timeout.tushare` 超时。HistorySyncService SHALL 经 Registry 获取数据，SHALL NOT 直接实例化具体 Provider 或 import tushare。metrics source key SHALL 使用 `tushare_history_stock_basic` / `tushare_history_daily` / `tushare_history_adj_factor` / `tushare_history_daily_basic` / `tushare_history_moneyflow` / `tushare_history_stock_company` / `tushare_history_namechange`（或统一 `tushare_history` 前缀 + endpoint 日志字段；交易日历不在此列——trade_cal 由现有 TushareTradingCalendarProvider 扩展提供，不新增第二个 trade_cal Provider），复用现有 `ProviderMetricsRegistry`，SHALL NOT 新建历史专属 metrics/timeout 体系。

#### Scenario: 配置选源

- **WHEN** config.yaml 配置 providers.history 选择 tushare
- **THEN** Registry 构造 TushareHistoricalMarketDataProvider 单例，Service 经 Registry 调用

#### Scenario: 指标进入现有体系

- **WHEN** 历史数据集请求发生
- **THEN** ProviderMetricsRegistry 中对应 tushare_history_* key 的 request/success/error/timeout 计数更新，无第二套统计实现

### Requirement: 共享 Tushare transport

`TushareHistoricalMarketDataProvider` SHALL 经 `app/providers/tushare_common.py` 的 `create_tushare_pro_client(config)` 与 `TushareRequestGate` 发起全部 Tushare 请求（请求节奏要求见 provider-metrics 能力），SHALL NOT 自建独立 client 或绕过 gate。调用层级 SHALL 固定为：Registry 的 `call_with_metrics`（超时与指标）→ Provider 方法（fields/参数/校验/normalize）→ RequestGate（进程级节奏）→ Tushare SDK，三者职责不可互相替代。

#### Scenario: 并发调用被限速

- **WHEN** 历史 job 与估值 job 同一时刻发起 Tushare 请求
- **THEN** 两请求经同一 RequestGate 按 endpoint 最小间隔串行发出，不并发撞限流

### Requirement: 显式字段声明

具体 Tushare Provider SHALL 为每个数据集定义 EXPECTED_FIELDS / REQUIRED_FIELDS / OPTIONAL_FIELDS 常量（如 TUSHARE_DAILY_FIELDS），对支持 `fields` 的接口显式传入全部期望字段，SHALL NOT 依赖 Tushare 默认返回列。Tushare 未来新增字段 SHALL NOT 自动进入数据库（须经显式 migration + fields 更新）。

#### Scenario: 新增上游字段不自动入库

- **WHEN** Tushare 为 daily 接口新增返回列而 Provider 字段清单未更新
- **THEN** 写入内容不变，schema 不漂移，应用不报错

### Requirement: 接口上限截断防护

Provider SHALL 识别已知 API 行数上限：`daily`/`daily_basic`/`moneyflow` 单次返回恰好 6000 行时 SHALL 置 `truncation_risk=True` 并在不能确认完整时拒绝提交；`stock_company` 上限 4500，`stock_basic` 上限 6000。`stock_basic` SHALL 主动按 exchange（SSE/SZSE/BSE）× list_status（L/D/P/G/UN）分片获取（空分片允许，任一非空分片命中上限则本轮主档刷新失败）；`stock_company` SHALL 按 exchange 分片；`adj_factor` SHALL 对异常固定阈值保持保守。截断 fallback SHALL 采用更细粒度请求合并去重后复检；对文档未明确保证多代码参数的接口 SHALL NOT 凭猜测使用批量参数（须经在线 smoke test 确认）。

在线实测（2026-09-19，真实 Token）确认：`daily`/`moneyflow`/`adj_factor` 支持逗号分隔多 `ts_code`；`daily_basic` 对多代码**静默返回 0 行**（不报错）。因此 `daily_basic` 的截断 fallback SHALL 按**候选集 − 已返回代码**逐只查询：候选集由 `cn_stock_basic` 的 `list_date`/`delist_date` 生成该交易日可能产生行情的证券集合；每个缺失证券 SHALL 得到"有记录"或**明确空结果**（停牌等自然缺失允许为空）；任一请求异常则该交易日 SHALL NOT 判为 COMPLETE 且水位 SHALL NOT 推进。SHALL NOT 复用其他接口的 multi-code fallback，SHALL NOT 依赖官方文档未声明的 `offset`/`limit` 分页。

#### Scenario: 恰好 6000 行不提交

- **WHEN** daily 某交易日返回恰好 6000 行且 fallback 后仍无法确认完整
- **THEN** 该日校验失败（TRUNCATION_RISK），水位不推进

#### Scenario: daily_basic 多代码静默空不得掩盖缺失

- **WHEN** daily_basic 某交易日返回恰好 6000 行，fallback 以逐只请求补齐"候选集 − 已返回代码"
- **THEN** 补齐 SHALL 逐只请求（SHALL NOT 合并为逗号分隔的多代码参数），每个缺失证券得到有记录或明确空结果
- **AND** 任一补齐请求异常时该交易日不 COMPLETE、水位不推进，按可重试错误进入重试

#### Scenario: stock_basic 分片避免截断

- **WHEN** 同步 stock_basic
- **THEN** 按 3 交易所 × 5 上市状态分片逐个请求，单分片不触达 6000 上限即全部成功合并提交

### Requirement: ts_code 到 instrument_id 映射

Provider SHALL 基于 `cn_stock_basic` 将 Tushare `ts_code` 映射为 MarketMind `instrument_id`（`CN:STOCK:<symbol>`，symbol 取 ts_code 代码部分），SHALL NOT 通过证券代码首位推断交易所，SHALL 使用 Tushare 返回的 exchange。事实数据出现主档未知的 ts_code 时 SHALL 触发一次 stock_basic 刷新后重新映射；仍未知 SHALL 以 UNKNOWN_INSTRUMENT 拒绝该日提交，SHALL NOT 创建占位证券。

#### Scenario: BSE 证券正确映射

- **WHEN** Tushare 返回北交所证券 ts_code=833533.BJ、exchange=BSE
- **THEN** 映射为 CN:STOCK:833533，exchange 记为 BSE，不按代码首位推断

#### Scenario: 未知代码拒绝提交

- **WHEN** 某 daily 返回主档中不存在的 ts_code 且刷新 stock_basic 后仍不存在
- **THEN** 该交易日校验失败（UNKNOWN_INSTRUMENT），不创建占位 instrument，水位不动
