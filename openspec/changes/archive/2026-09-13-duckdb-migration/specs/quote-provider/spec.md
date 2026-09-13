## ADDED Requirements

### Requirement: QuoteProvider 统一接口

系统 SHALL 定义 QuoteProvider 接口：`get_quotes(instruments: list[Instrument]) -> dict[instrument_id, Quote]`；业务层 SHALL 只通过该接口获取行情，禁止直接调用 AKShare 或触碰 DataFrame。V1 SHALL 覆盖 A股股票、A股ETF、港股股票、港股ETF、A股指数、港股指数六类资产。

#### Scenario: 统一返回模型
- **WHEN** Provider 获取任一类型资产行情
- **THEN** 返回内部 Quote 模型（instrument_id、price、change_percent、volume_ratio、previous_close、source、source_timestamp），不暴露第三方列名

#### Scenario: 单资产类型缺失字段
- **WHEN** 数据源不提供量比（如指数）
- **THEN** Quote 中对应字段为 None，不伪造数据

### Requirement: 第三方字段清洗

Provider 层 SHALL 将 `"-"`、`""`、`None`、`NaN`、`inf` 统一转换为 `None`；API JSON 输出 SHALL NOT 包含 NaN/Infinity。

#### Scenario: 脏值转 None
- **WHEN** AKShare 返回 PE 或价格为 "-"
- **THEN** 转换后为 None

#### Scenario: 数值安全
- **WHEN** 外部返回 NaN/inf
- **THEN** 转换后为 None

### Requirement: 全市场接口过滤

当 AKShare 接口只能一次获取全市场行情时，Provider SHALL 在内存中过滤仅保留用户关注的标的，不保存其他证券数据。

#### Scenario: 过滤
- **WHEN** 全市场接口返回 5000 条而自选只有 2 只
- **THEN** 仅返回这 2 只的 Quote，其余丢弃

### Requirement: 稳定性与容错

Provider SHALL 设置请求超时；单次任务最多重试 1 次；单个市场失败 SHALL NOT 影响其他市场，且失败时 SHALL 保留缓存中最后一次成功数据。港股行情存在延迟时 SHALL 标记 delayed。

#### Scenario: 港股接口失败
- **WHEN** AKShare 港股请求抛出异常
- **THEN** A股数据正常返回，港股保留最后缓存，日志记录失败原因，服务不崩溃

#### Scenario: 不无限重试
- **WHEN** 连续失败
- **THEN** 单轮任务内最多重试 1 次后放弃该市场本轮刷新

### Requirement: Provider 可替换

行情与估值数据源 SHALL 通过 config.yaml 的 providers 配置项声明，代码结构 SHALL 支持后续新增 Provider 实现，无需改动业务层。

#### Scenario: 配置声明数据源
- **WHEN** 读取 config.yaml
- **THEN** providers.quote 按 cn_stock/cn_etf/hk_stock/hk_etf/cn_index/hk_index 声明数据源，providers.fundamental.cn_stock 声明估值源
