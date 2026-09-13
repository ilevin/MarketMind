# watchlist-management Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 股票/ETF 自选添加

系统 SHALL 提供 `POST /api/watchlist`，接收 symbol、market、asset_type；仅允许 STOCK/ETF；自动识别名称后写入 instrument 与 watchlist。添加成功后 SHALL 无论市场状态如何（OPEN/LUNCH_BREAK/CLOSED/HOLIDAY）均触发一次该资产行情更新；即时刷新失败 SHALL NOT 影响添加结果。添加 CN/STOCK 成功后 SHALL 同时立即获取该股最近一期估值，估值获取失败 SHALL NOT 影响添加结果。

#### Scenario: 添加成功
- **WHEN** POST `{symbol:"600519", market:"CN", asset_type:"STOCK"}` 且证券可识别
- **THEN** 返回 201 Created

#### Scenario: 重复添加
- **WHEN** instrument_id 已在 watchlist
- **THEN** 返回 409 Conflict

#### Scenario: 证券不存在
- **WHEN** 代码无法识别
- **THEN** 返回 404 Not Found 及明确错误信息

#### Scenario: 拒绝指数类型
- **WHEN** asset_type 为 INDEX
- **THEN** 返回校验错误，不写入

#### Scenario: 休市时段添加后立即获取行情
- **WHEN** 市场 CLOSED（或 LUNCH_BREAK/HOLIDAY）时 POST 添加自选成功
- **THEN** 系统对该标的执行一次行情更新，页面能显示最近收盘行情

#### Scenario: 即时刷新失败不影响添加
- **WHEN** 添加成功但行情 Provider 抛异常或超时
- **THEN** 添加接口仍返回 201，刷新失败仅记录日志

#### Scenario: 添加后立即获取估值
- **WHEN** 添加 CN/STOCK 自选成功
- **THEN** 系统立即获取该股最近一期估值并写入，页面无需等待收盘后刷新即可显示

#### Scenario: 估值获取失败不影响添加
- **WHEN** 添加成功但估值 Provider 抛异常或超时
- **THEN** 添加接口仍返回 201，失败仅记录日志

### Requirement: 股票/ETF 自选删除

系统 SHALL 提供 `DELETE /api/watchlist/{instrument_id}`，仅删除 watchlist 记录，保留 instrument/quote/fundamental 历史数据。删除自选条目时其标签关联 SHALL 在同一写锁内一并清理（先删关联提交、再删条目提交，见 database-persistence 外键与删除行为）；对外可观察行为不变：删除后该条目的标签关联消失、对应标签 usage_count 递减。

#### Scenario: 删除成功
- **WHEN** 删除一个存在的自选
- **THEN** 返回 204 No Content，历史行情快照仍在数据库

### Requirement: 股票/ETF 自选查询与排序

系统 SHALL 提供 `GET /api/watchlist`（按 sort_order 返回列表）与 `PUT /api/watchlist/order`（批量更新排序）。列表 items SHALL 含 instrument_id、symbol、name、market、asset_type、sort_order、tags（`[{id, name}]` 数组，空数组表示无标签）。

#### Scenario: 查询列表
- **WHEN** GET /api/watchlist
- **THEN** 返回含 instrument_id、symbol、name、market、asset_type、sort_order、tags 的 items，无标签条目 tags 为空数组

#### Scenario: 调整排序
- **WHEN** PUT /api/watchlist/order 传入新的 instrument_id/sort_order 列表
- **THEN** 排序持久化，GET 按新顺序返回

### Requirement: 指数配置

系统 SHALL 提供 `GET/POST/DELETE /api/index-watchlist` 与 `PUT /api/index-watchlist/order`，行为与股票/ETF 自选一致，但仅允许 INDEX 类型，存储于独立的 index_watchlist 表。添加成功后 SHALL 无论市场状态如何均触发一次该指数行情更新；即时刷新失败 SHALL NOT 影响添加结果。

#### Scenario: 添加指数
- **WHEN** POST `{symbol:"000300", market:"CN", asset_type:"INDEX"}`
- **THEN** 返回 201，名称自动识别

#### Scenario: 禁止股票/ETF 进入指数配置
- **WHEN** POST /api/index-watchlist 时 asset_type 为 STOCK 或 ETF
- **THEN** 返回校验错误

#### Scenario: 指数重复添加
- **WHEN** 指数已在 index_watchlist
- **THEN** 返回 409 Conflict

#### Scenario: 休市时段添加指数后立即获取行情
- **WHEN** 市场 CLOSED（或 LUNCH_BREAK/HOLIDAY）时 POST 添加指数成功
- **THEN** 系统对该指数执行一次行情更新，页面能显示最近收盘行情

#### Scenario: 即时刷新不改变周期刷新策略
- **WHEN** 市场 CLOSED/LUNCH_BREAK/HOLIDAY 时发生添加操作触发的即时刷新
- **THEN** 该一次性刷新执行后，60 秒后台任务仍遵守「仅 OPEN 市场刷新」策略，不对该市场做周期性行情请求

### Requirement: 自选管理页面

系统 SHALL 提供 `/watchlist` 页面（Jinja2），包含股票/ETF 与指数两个独立管理区域，支持查看、添加、删除、调整排序操作。指数添加区域 SHALL 提示港股指数代码格式（字母缩写，示例含恒生科技指数 HSTECH）。股票/ETF 区域 SHALL 提供「标签」列：以行内可点击的标签 chips 展示全部已有标签（已关联高亮、未关联置灰，SHALL NOT 支持在此新建标签），点击 chip 即切换该标签的关联并保存；指数区域 SHALL NOT 出现标签列。

#### Scenario: 空状态
- **WHEN** 无任何自选时访问页面
- **THEN** 显示空状态提示，不报错

#### Scenario: 港股指数代码提示
- **WHEN** 访问 /watchlist 指数管理区域
- **THEN** 输入框提示同时包含 A 股与港股指数代码示例（如 000001 上证指数、HSTECH 恒生科技指数）

#### Scenario: 点击 chip 切换关联
- **WHEN** 在股票/ETF 行点击置灰的「科技」chip
- **THEN** 调用关联 API 保存成功，该 chip 变为高亮（已关联）

#### Scenario: 点击 chip 取消关联
- **WHEN** 点击已高亮的「科技」chip
- **THEN** 该条目解除与「科技」的关联，chip 变为置灰

#### Scenario: chips 仅含已有标签
- **WHEN** 渲染标签列
- **THEN** chips 仅含标签管理中已存在的标签，无输入新建入口

### Requirement: 数据持久化

自选与指数配置 SHALL 持久化到 DuckDB；服务重启后数据仍存在。

#### Scenario: 重启后保留
- **WHEN** 添加自选后重启服务
- **THEN** 自选列表与指数配置完整保留

### Requirement: 自选条目标签关联 API

系统 SHALL 提供 `PUT /api/watchlist/{instrument_id}/tags`，body 为 `{"tag_ids": [int...]}`：以全量集合替换该条目的全部标签关联（幂等；空数组即解除全部标签）。一个自选条目 SHALL 可同时关联多个标签（多对多）。自选条目不存在或任一 tag_id 不存在返回 404；指数类型返回 400。

#### Scenario: 股票设置多个标签
- **WHEN** PUT `/api/watchlist/CN:STOCK:600519/tags` `{"tag_ids": [3, 5]}`
- **THEN** 返回 200，该自选条目同时关联标签 3 与 5

#### Scenario: ETF 设置标签
- **WHEN** PUT ETF 自选条目 tags `{"tag_ids": [3]}`
- **THEN** 返回 200，该 ETF 关联标签 3

#### Scenario: 全量替换语义
- **WHEN** 条目已关联 [1, 2]，PUT `{"tag_ids": [2, 4]}`
- **THEN** 条目改为关联且仅关联 [2, 4]

#### Scenario: 解除全部标签
- **WHEN** 已有标签的条目 PUT tags `{"tag_ids": []}`
- **THEN** 返回 200，该条目 tags 为空

#### Scenario: 标签不存在
- **WHEN** PUT tags 传入含不存在 id 的数组
- **THEN** 返回 404

#### Scenario: 指数拒绝设置标签
- **WHEN** 对指数条目设置标签
- **THEN** 返回 400，指数不支持标签

