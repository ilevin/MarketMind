## ADDED Requirements

### Requirement: stock_basic 主档同步映射

历史同步刷新 stock_basic 时 SHALL 将每个证券 upsert 到现有 `instrument` 表并保存原始字段到 `cn_stock_basic`，映射规则固定：`instrument_id = "CN:STOCK:" + symbol`、market="CN"、asset_type="STOCK"、name=stock_basic.name、exchange=stock_basic.exchange、currency="CNY"、`is_active = (list_status == "L")`。exchange SHALL 取自 Tushare 返回值，SHALL NOT 通过证券代码首位推断 SH/SZ/BJ。证券退市时 SHALL 仅置 `instrument.is_active=false` 与 `cn_stock_basic.list_status="D"`，SHALL NOT 删除 instrument、cn_stock_basic 或任何历史事实数据；主档刷新 SHALL NOT 因某历史证券未出现在本轮结果而物理删除该证券。

#### Scenario: 新证券入库

- **WHEN** stock_basic 刷新返回新上市证券 301999.SZ
- **THEN** instrument 插入 CN:STOCK:301999（exchange=SZSE、is_active=true），cn_stock_basic 保存全部业务字段

#### Scenario: 退市证券保留

- **WHEN** 已有 instrument CN:STOCK:600001 且该证券已退市（list_status=D）
- **THEN** instrument 保留且 is_active=false，历史事实数据不受影响，watchlist 等引用不被删除

#### Scenario: 重复刷新幂等

- **WHEN** stock_basic 连续两次成功刷新
- **THEN** instrument 与 cn_stock_basic 无重复行，名称等信息按最新值更新
