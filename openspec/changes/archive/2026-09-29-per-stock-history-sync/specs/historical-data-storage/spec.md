## MODIFIED Requirements

### Requirement: 日级历史事实表

系统 SHALL 为四个日级数据集分别建立独立事实表：`market_daily_bar`（日线行情）、`market_adj_factor`（复权因子）、`market_daily_basic`（每日指标）、`market_moneyflow`（个股资金流）。业务唯一键 SHALL 为 `(instrument_id, trade_date)`，由单股区间替换（WriteCoordinator 写锁内 DELETE 该股区间旧行 + 批量 INSERT 新行）与应用层校验保证，SHALL NOT 建立物理主键/外键/二级索引。交易日期 SHALL 使用 DATE 类型；数值字段 SHALL 优先使用 DOUBLE（`limit_status` 使用 SMALLINT、资金流 `*_vol` 使用 BIGINT、`employees` 使用 INTEGER）。每行 SHALL 携带采集元数据 `source` 与 `fetched_at`（TIMESTAMPTZ），SHALL NOT 在事实行上保存 run_id（执行归属经 `sync_task` 查询）。表结构与业务字段在本变更中 SHALL NOT 改变。

#### Scenario: 四表独立存在

- **WHEN** 执行数据库迁移后检查表清单
- **THEN** 存在 market_daily_bar、market_adj_factor、market_daily_basic、market_moneyflow 四张表，列与技术方案字段清单一致（含 daily 的 ah_vol/ah_amount 可空列、daily_basic 的 limit_status、moneyflow 的 16 个买卖字段与 net_mf_*）

#### Scenario: 事实表无 ORM 约束

- **WHEN** 检查四张事实表定义
- **THEN** 无物理主键、无外键、无二级索引，唯一性由"单股区间 DELETE + INSERT（写锁内）+ 应用层校验"共同保证

#### Scenario: 区间替换不产生重复

- **WHEN** 同一股票同一区间被两次成功同步
- **THEN** 第二次以区间替换覆盖第一次的数据，(instrument_id, trade_date) 无重复行

### Requirement: 事实表统计维护

`history_sync_state` SHALL 事务内维护每个数据集的 `record_count`、`data_min_date`、`data_max_date`（单股区间替换时按 new_count - old_count 增减），管理员页面与 API SHALL 读取该小表获取统计，SHALL NOT 在请求路径对千万级事实表执行 `COUNT(*)` 或 `MAX(trade_date)` 全表扫描。

#### Scenario: 区间替换后计数正确

- **WHEN** 某股票某区间原已写入 4000 行，重新区间替换为 4010 行并提交
- **THEN** 该数据集 record_count 净增 10，data_min_date/data_max_date 与事实表实际范围一致

#### Scenario: 管理员页面不扫大表

- **WHEN** 管理员刷新 /admin/data 页面
- **THEN** summary API 仅读 history_sync_state、stock_sync_state 等小表即返回 record_count 与数据范围
