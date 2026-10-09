## ADDED Requirements

### Requirement: ETF universe 主档同步映射

ETF universe 刷新（数据集 `etf_basic`，来源 AKShare/东方财富 ETF 列表）SHALL 将每个当前上市 ETF upsert 到现有 `instrument` 表并保存业务字段到 `cn_etf_basic`，映射规则固定：`instrument_id = "CN:ETF:" + symbol`、market="CN"、asset_type="ETF"、name=列表名称、exchange 取列表接口显式市场信息（接口无市场列时按 ETF 代码首位推断：5 开头→SSE、1 开头→SZSE——ETF 交易所映射无歧义，腾讯实时通道已用同一规则；SHALL NOT 将该规则用于股票）、currency="CNY"、`is_active=true`。`cn_etf_basic` SHALL 同事务保存 instrument_id、ts_code（symbol + 交易所后缀，Tushare 口径如 `510300.SH`）、symbol、name、exchange、list_date（接口提供则保存，否则 NULL）、delist_date（DATE，可空——对齐 `cn_stock_basic` 双列模式，V1 东财列表无退市日期来源、恒为 NULL）、source、fetched_at、source_last_seen_at、sync_run_id。本轮列表未出现的已有 ETF instrument SHALL 仅置 `is_active=false`（不删除 instrument、cn_etf_basic 或任何历史事实数据，watchlist 等引用不受影响）；重新出现时 SHALL 恢复 `is_active=true`。重复刷新 SHALL 幂等（无重复行，名称等信息按最新值更新）。

#### Scenario: 新 ETF 入库

- **WHEN** universe 刷新返回新上市 ETF 588999（上交所）
- **THEN** instrument 插入 CN:ETF:588999（exchange=SSE、is_active=true），cn_etf_basic 保存 ts_code=588999.SH 与全部业务字段

#### Scenario: 深市 ETF 代码首位映射

- **WHEN** universe 刷新返回 ETF 159915（列表接口无显式市场列）
- **THEN** exchange 映射为 SZSE、ts_code 构造为 159915.SZ，不按沪市处理

#### Scenario: 退市 ETF 保留

- **WHEN** 已有 instrument CN:ETF:510600 且该 ETF 不在本轮 universe 列表中
- **THEN** instrument 保留且 is_active=false，cn_etf_basic 与历史事实数据不受影响，自选引用不被删除

#### Scenario: 重复刷新幂等

- **WHEN** universe 连续两次成功刷新且列表相同
- **THEN** instrument 与 cn_etf_basic 无重复行，名称等按最新值更新，is_active 不变
