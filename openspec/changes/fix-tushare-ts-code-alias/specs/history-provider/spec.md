## ADDED Requirements

### Requirement: 历史 ts_code 别名规范化

Provider SHALL 在把 Tushare 历史事实的 `ts_code` 映射为 `instrument_id` 之前，把**已登记**的历史证券代码改写为该证券当前的规范代码（登记表 `TUSHARE_TS_CODE_ALIASES`，首条 `000022.SZ -> 001872.SZ`：深赤湾A 于 2018-12-26 变更为招商港口）。规范化 SHALL 只作用于四个日级事实数据集（daily / adj_factor / daily_basic / moneyflow），SHALL NOT 作用于 `stock_basic` / `stock_company` / `namechange`——主档是 `instrument` 的来源，改写主档会造出第二条证券记录。

Provider SHALL NOT 按 `.SZ` / `.SH` / `.BJ` 后缀、代码段或名称相似度推断别名，SHALL NOT 因为某代码不在主档就放行；未登记的未知 `ts_code` SHALL 仍按 `UNKNOWN_INSTRUMENT` 拒绝该日提交、不推进水位。

`ProviderBatch.raw_row_count` SHALL 记录规范化**之前**的上游原始行数，SHALL NOT 因合并而"美化"监控口径。

#### Scenario: 仅返回旧代码

- **WHEN** 某交易日 daily 只返回 `ts_code=000022.SZ`，主档只有 `CN:STOCK:001872`
- **THEN** 该行 `ts_code` 被改写为 `001872.SZ`、映射为 `CN:STOCK:001872`，该交易日正常提交、水位推进，SHALL NOT 抛 `UNKNOWN_INSTRUMENT`，SHALL NOT 创建 `CN:STOCK:000022` 证券

#### Scenario: 新旧代码并存且字段一致

- **WHEN** 同一交易日同一 endpoint 同时返回 `000022.SZ` 与 `001872.SZ`，且两者业务字段一致
- **THEN** Provider 只交付一行（规范代码 `001872.SZ`），`raw_row_count` 仍为 2，并记录含 `action=drop_legacy` 的 WARNING 日志

#### Scenario: 新旧代码并存但字段冲突

- **WHEN** 同一交易日同一 endpoint 同时返回 `000022.SZ` 与 `001872.SZ`，且至少一个业务字段取值不同
- **THEN** Provider 抛 `ALIAS_CONFLICT`（TushareError 子类），该交易日 SHALL NOT 提交、水位 SHALL NOT 推进，异常文本含 endpoint、交易日与两个 ts_code 以及不一致字段

#### Scenario: 未登记的未知代码仍被拒绝

- **WHEN** 事实数据返回主档中不存在的 `ts_code=999999.SZ` 且该代码未登记为别名
- **THEN** 仍按 `UNKNOWN_INSTRUMENT` 拒绝该日提交，不创建占位证券，水位不动

#### Scenario: 两条抓取路径行为一致

- **WHEN** 主路径命中 6000 行上限走逐证券 fallback，且上游对该证券的历史日期仍返回旧代码
- **THEN** fallback 与主路径使用同一规范化口径，产出的 `instrument_id` 与规范 `ts_code` 与主路径完全一致

#### Scenario: 主档数据集不参与别名改写

- **WHEN** `stock_basic` 的分片返回 `ts_code=000022.SZ`
- **THEN** 该记录原样交付（`instrument_id=CN:STOCK:000022`），由 Service 按 `list_status` 处置，Provider SHALL NOT 在此改写为主档中的其它代码
