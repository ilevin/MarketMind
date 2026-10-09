## ADDED Requirements

### Requirement: ETF 日级事实表

系统 SHALL 为两个 ETF 日级数据集建立独立事实表：`etf_daily`（东方财富原始日线）、`etf_adj_factor`（Tushare 复权因子）。业务唯一键 SHALL 为 `(instrument_id, trade_date)`，由单券区间替换（WriteCoordinator 写锁内 DELETE 该券区间旧行 + 批量 INSERT 新行）与应用层校验保证，SHALL NOT 建立物理主键/外键/二级索引（与四张股票事实表同惯例）。交易日期 SHALL 使用 DATE 类型；价格/成交额/换手率/复权因子 SHALL 使用 DOUBLE、成交量 SHALL 使用 BigInteger（对齐 `market_moneyflow.*_vol` 先例）。每行 SHALL 携带采集元数据 `source`（取值 `eastmoney`/`tushare`，小写，对齐项目惯例）与 `fetched_at`（TIMESTAMPTZ）。`etf_daily` 列 SHALL 为 instrument_id、ts_code（冗余展示列）、trade_date、open/high/low/close、volume、amount、turnover_rate、source、fetched_at；`etf_adj_factor` 列 SHALL 为 instrument_id、ts_code、trade_date、adj_factor（NOT NULL）、source、fetched_at。上游原始单位 SHALL 原样保存（东财成交量/成交额/换手率按接口原始单位，单位口径经在线实测固化进 Provider 文档），单位换算 SHALL 只发生在展示/查询层。复权价格 SHALL NOT 入库（复用既有"不存储派生复权数据"要求，ETF 表同受约束）。

#### Scenario: 两表独立存在

- **WHEN** 执行 0005 迁移后检查表清单
- **THEN** 存在 etf_daily 与 etf_adj_factor 两张表，列与字段清单一致（etf_adj_factor.adj_factor 非空）

#### Scenario: 事实表无 ORM 约束

- **WHEN** 检查两张 ETF 事实表定义
- **THEN** 无物理主键、无外键、无二级索引，唯一性由"单券区间 DELETE + INSERT（写锁内）+ 应用层校验"共同保证

#### Scenario: 区间替换不产生重复

- **WHEN** 同一 ETF 同一区间被两次成功同步
- **THEN** 第二次以区间替换覆盖第一次的数据，(instrument_id, trade_date) 无重复行

### Requirement: cn_etf_basic 主档表

系统 SHALL 保存 ETF 业务主档到 `cn_etf_basic`：instrument_id 主键 + 外键约束→instrument.instrument_id（迁移定义沿 0003 `cn_stock_basic` 先例传入显式命名，DuckDB 实际落库为方言规范化名）、ts_code、symbol、name、exchange、list_date（DATE，可空——接口未提供上市日期时保存 NULL，由同步 planner 保守回退）、delist_date（DATE，可空——对齐 `cn_stock_basic` 双列模式，V1 东财列表无退市日期来源、恒为 NULL，供 planner 生命周期边界使用）、source、fetched_at、source_last_seen_at、sync_run_id。主档 SHALL 覆盖 universe 刷新返回的全部当前上市 ETF，SHALL NOT 因 is_active=false 移除既有行。上市日期缺失的 ETF SHALL 以 NULL 落库，SHALL NOT 人工推算填充。

#### Scenario: 主档行随 universe 维护

- **WHEN** universe 刷新成功后查询 cn_etf_basic
- **THEN** 每个当前上市 ETF 一行，字段与列表接口口径一致，无重复

#### Scenario: 上市日期缺失保存 NULL

- **WHEN** 列表接口未返回某 ETF 的上市日期
- **THEN** cn_etf_basic.list_date 为 NULL，同步起点由 planner 按 history.start_date 保守回退
