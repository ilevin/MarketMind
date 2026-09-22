# historical-data-storage Specification

## Purpose
TBD - created by archiving change a-share-historical-data. Update Purpose after archive.
## Requirements
### Requirement: 日级历史事实表

系统 SHALL 为四个日级数据集分别建立独立事实表：`market_daily_bar`（日线行情）、`market_adj_factor`（复权因子）、`market_daily_basic`（每日指标）、`market_moneyflow`（个股资金流）。业务唯一键 SHALL 为 `(instrument_id, trade_date)`，由整日替换与应用层校验保证，SHALL NOT 建立物理主键/外键/二级索引。交易日期 SHALL 使用 DATE 类型；数值字段 SHALL 优先使用 DOUBLE（`limit_status` 使用 SMALLINT、资金流 `*_vol` 使用 BIGINT、`employees` 使用 INTEGER）。每行 SHALL 携带采集元数据 `source` 与 `fetched_at`（TIMESTAMPTZ），SHALL NOT 在事实行上保存 run_id（执行归属经 `history_day_status.completed_by_run_id` 查询）。

#### Scenario: 四表独立存在

- **WHEN** 执行数据库迁移后检查表清单
- **THEN** 存在 market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow 四张表，列与技术方案字段清单一致（含 daily 的 ah_vol/ah_amount 可空列、daily_basic 的 limit_status、moneyflow 的 16 个买卖字段与 net_mf_*）

#### Scenario: 事实表无 ORM 约束

- **WHEN** 检查四张事实表定义
- **THEN** 无物理主键、无外键、无二级索引，唯一性由"整日 DELETE + INSERT + history_day_status 账本"共同保证

### Requirement: 原始字段完整保存

系统 SHALL 按 Tushare 当前接口可获取口径完整保存原始业务字段：支持 `fields` 参数的接口 SHALL 显式声明全部期望字段（含默认不返回的可选字段，如 stock_basic 的 fullname/enname/exchange/curr_type/list_status/delist_date/is_hs、stock_company 的 introduction/office/main_business/business_scope、daily 的 ah_vol/ah_amount、daily_basic 的 limit_status），SHALL NOT 依赖默认返回列。原始单位 SHALL 保持 Tushare 官方单位不变（daily.vol 手、daily.amount 千元、daily_basic.total_mv 万元、moneyflow.*_amount 万元），单位换算 SHALL 只发生在查询/展示层。上游 NULL SHALL 原样保存（亏损导致的 PE NULL、历史阶段不存在的字段、公司资料可选字段），SHALL NOT 人工填充为 0 或其他值。

#### Scenario: 可选字段入库

- **WHEN** 同步 stock_basic 时 Tushare 返回包含 fullname/enname/is_hs 等可选字段
- **THEN** cn_stock_basic 保存这些字段值，未被请求的默认列不会隐式改变写入内容

#### Scenario: NULL 保留原义

- **WHEN** daily_basic 某日某证券 pe 为 NULL（亏损）或 daily 某历史行 ah_vol 不存在
- **THEN** 落库为 NULL，不转换为 0，校验不因此失败

#### Scenario: 单位不在入库层换算

- **WHEN** 检查事实表写入路径
- **THEN** vol 仍为手、amount 仍为千元、total_mv 仍为万元，无入库层单位换算逻辑

### Requirement: 不存储派生复权数据

系统 SHALL 长期保存原始 `daily` + `adj_factor`，SHALL NOT 将 qfq/hfq 复权价格或 `pro_bar` 输出作为事实表长期存储；复权价格 SHALL 在未来查询/分析层按需计算。

#### Scenario: 无复权事实表

- **WHEN** 检查数据库表清单
- **THEN** 不存在 qfq/hfq/pro_bar 派生事实表，market_adj_factor 仅保存原始复权因子

### Requirement: A股证券主档表

系统 SHALL 保存 Tushare 证券主档到 `cn_stock_basic`（17 个业务字段 + instrument_id 主键 + source/fetched_at/source_last_seen_at/sync_run_id）、公司资料到 `cn_stock_company`（含 introduction/office/main_business/business_scope 等全字段）、历史名称到 `cn_stock_name_change`（event_key 主键，为 ts_code+name+start_date 的 SHA-256 稳定键；同一 ts_code/name/start_date 出现多条不同事件时按在线验证结果扩展规范键，SHALL NOT 凭猜测改变键语义）。主档数据覆盖沪/深/北三市场与 Tushare 可提供的全部上市状态（含退市、暂停上市），SHALL NOT 裁剪到 2010 年起。

#### Scenario: 退市证券入主档

- **WHEN** stock_basic 返回 list_status=D 的退市证券
- **THEN** cn_stock_basic 保存该记录，instrument 同步存在且 is_active=false

#### Scenario: 曾用名完整保存

- **WHEN** namechange 返回某证券 2010 年前的历史名称记录
- **THEN** cn_stock_name_change 保存该记录，不因历史起点 2010-01-01 截断

### Requirement: trading_calendar 扩展

系统 SHALL 为现有 `trading_calendar` 表（主键 `(market, trade_date)` 保留）增加可空列 `exchange`、`pretrade_date`（DATE）、`source`、`fetched_at`（TIMESTAMPTZ）。现有实时市场状态代码路径 SHALL 继续只写 market/trade_date/is_open 三列且行为不变；历史严格日历路径 SHALL 主动填充全部新增列。历史同步所用日历数据 SHALL 满足 `market='CN'` 且 `source='tushare'`。

#### Scenario: 旧写入路径兼容

- **WHEN** 现有实时行情代码写 trading_calendar
- **THEN** 写入成功且不要求提供新增列（列可空），现有功能行为不变

#### Scenario: 历史日历带来源

- **WHEN** 历史同步刷新交易日历后读取
- **THEN** 行记录 exchange、pretrade_date、source='tushare'、fetched_at

### Requirement: 事实表统计维护

`history_sync_state` SHALL 事务内维护每个数据集的 `record_count`、`data_min_date`、`data_max_date`（整日替换时按 new_count - old_count 增减），管理员页面与 API SHALL 读取该小表获取统计，SHALL NOT 在请求路径对千万级事实表执行 `COUNT(*)` 或 `MAX(trade_date)` 全表扫描。

#### Scenario: 整日替换后计数正确

- **WHEN** 某交易日原已写入 5250 行，重新整日替换为 5260 行并提交
- **THEN** record_count 净增 10，data_min_date/data_max_date 与事实表实际范围一致

#### Scenario: 管理员页面不扫大表

- **WHEN** 管理员刷新 /admin/data 页面
- **THEN** summary API 仅读 history_sync_state 等小表即返回 record_count 与数据范围

