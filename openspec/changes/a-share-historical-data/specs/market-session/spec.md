## ADDED Requirements

### Requirement: 交易日历严格历史范围模式

现有 `TushareTradingCalendarProvider` SHALL 扩展提供严格历史范围读取能力（如 `get_days(market, start_date, end_date, *, strict=False)` 或等价接口）：`strict=False` 保持现有实时市场状态的行为（含既有 fallback 容错）；`strict=True` 时 SHALL 满足——数据必须来自 Tushare `trade_cal`、SHALL NOT 使用周一至周五近似、SHALL NOT 用既有 fallback 补造缺失日、上游不可用或不完整时 SHALL 抛出异常、返回并缓存 `pretrade_date` 与 source 元信息。历史数据同步 SHALL 只使用 strict 模式；现有实时行情"是否开市"逻辑 SHALL 保持原有容错策略不变。

#### Scenario: strict 模式无降级

- **WHEN** strict=True 且 Tushare trade_cal 不可用
- **THEN** 抛出异常，历史同步失败/等待，不以工作日近似推进水位

#### Scenario: 非 strict 行为不变

- **WHEN** 实时市场状态查询在 Tushare 不可用时
- **THEN** 保持现有 fallback 行为，现有市场状态功能与测试不受影响

#### Scenario: 范围读取与缓存

- **WHEN** strict 模式读取 2010-01-01 至 2010-12-31 的交易日
- **THEN** 返回该年度全部 is_open=1 日期（含 pretrade_date），并缓存到 trading_calendar（带 source='tushare'、fetched_at）
