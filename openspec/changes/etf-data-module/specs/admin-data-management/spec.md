## REMOVED Requirements

### Requirement: ETF 数据占位页面
**Reason**: ETF 数据模块（本变更）落地真实功能——`/admin/data/etf` 由占位说明升级为 ETF 数据管理总览页，占位语义不再成立。
**Migration**: 路由 `/admin/data/etf`、模板 `admin_data_etf.html` 与认证/导航行为保留，内容区由「敬请期待」占位改写为真实数据总览（见 ADDED「ETF 数据管理页面」）；相关占位测试改写为真实页面测试。

### Requirement: ETF 历史占位页面
**Reason**: ETF 历史数据能力（本变更）落地——`/admin/data/etf/history` 由占位说明升级为 ETF 个股历史管理页，占位语义不再成立。
**Migration**: 路由 `/admin/data/etf/history`、模板 `admin_data_etf_history.html` 与认证/导航行为保留，内容区改写为个股历史列表（见 ADDED「ETF 个股历史页面」）；相关占位测试改写为真实页面测试。

## MODIFIED Requirements

### Requirement: overall_status 计算

管理员页面与 summary 的 overall_status SHALL 动态计算，优先级：有 active run → RUNNING；上次 Run 系统级 FAILED → ERROR；无 FAILED 但任一核心日级数据集存在落后（lagging）股票 → LAGGING（"存在缺口"；个股失败 SHALL NOT 单独升级整体 ERROR）；四个核心日级数据集全部股票 up_to_date 且无 active run → HEALTHY。stock_company/namechange 短暂失败 SHALL 以 warning 呈现但不必然升级整体 ERROR；trade_cal/stock_basic 失败 SHALL 升级整体异常级别。ETF 数据集（`history.etf_enabled=true` 时）SHALL 纳入同一优先级链：etf_basic/etf_daily/etf_adj_factor 任一数据集级 FAILED SHALL 升级 ERROR（东财/fund_adj 系统性不可用值得整体告警）；ETF 日级数据集存在落后 ETF SHALL 计入 LAGGING；`etf_enabled=false` 时 ETF 数据集 SHALL 完全不参与判定（股票判定逻辑零改动）。

#### Scenario: 个股失败不升级整体异常

- **WHEN** moneyflow 有 2 只股票 task failed 而其余全部追平
- **THEN** overall_status 为 LAGGING（数据集 lagging_count=2），不为 ERROR，页面同时显示整体正常与缺口明细

#### Scenario: 全部追平

- **WHEN** 四个股票日级数据集与两个 ETF 日级数据集全部 up_to_date 且无 active run
- **THEN** overall_status 为 HEALTHY

#### Scenario: ETF 数据源系统性失败升级 ERROR

- **WHEN** etf_basic 刷新连续失败（数据集级 FAILED）而股票数据集全部正常
- **THEN** overall_status 为 ERROR，页面同时展示股票部分健康与 ETF 失败明细

#### Scenario: ETF 缺口计入 LAGGING

- **WHEN** 全部股票数据集 up_to_date、etf_daily 有 5 只 ETF 落后且无 FAILED
- **THEN** overall_status 为 LAGGING

#### Scenario: ETF 关闭不参与判定

- **WHEN** history.etf_enabled=false 且股票数据集全部追平
- **THEN** overall_status 为 HEALTHY，ETF 数据集不参与任何分支

### Requirement: 个股列表 API

`GET /api/admin/history-data/stocks?dataset=&status=all|success|failed&q=&page=` SHALL 仅要求管理员：dataset 必填且仅接受六个日级数据集名（daily/adj_factor/daily_basic/moneyflow/etf_daily/etf_adj_factor）；page_size SHALL 服务端固定为 100（客户端传入更大值被钳制）；查询 SHALL 为"主档表 LEFT JOIN stock_sync_state（按 dataset 过滤）"，JOIN 目标 SHALL 按 dataset 分派——股票数据集 JOIN `cn_stock_basic`、ETF 数据集 JOIN `cn_etf_basic`（响应字段结构两者一致：ts_code、名称、watermark_date、last_status、last_success_at、last_error_code、last_task_id）；状态筛选与名称/代码搜索（LIKE）SHALL 在 SQL 内完成，SHALL NOT 将全量证券加载到浏览器。默认排序 SHALL 为 `last_status='failed' 优先 → watermark_date 升序（NULL 最前）→ ts_code 升序`。响应 SHALL 包含分页列表（ts_code、名称、watermark_date、last_status、last_success_at、last_error_code、last_task_id——供失败行直连任务详情 API，股票与 ETF 数据集字段结构一致）、分页元信息（总数、当前页、总页数）及该数据集统计块（与 summary 同口径）。

#### Scenario: 分页与排序

- **WHEN** 管理员请求 dataset=daily 无筛选的第 1 页
- **THEN** 返回按默认排序的前 100 条与分页元信息；page=2 返回后续 100 条

#### Scenario: ETF 数据集分派到 cn_etf_basic

- **WHEN** 管理员请求 dataset=etf_daily&q=沪深
- **THEN** 查询经 cn_etf_basic JOIN stock_sync_state 完成名称搜索，仅返回名称含"沪深"的 ETF 分页列表

#### Scenario: 无效参数拒绝

- **WHEN** 请求 dataset=trade_cal 或 page=0
- **THEN** 返回 422，不产生查询

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户请求该 API
- **THEN** 普通用户 403、未登录 401

### Requirement: 前端轮询

数据管理分区各页（/admin/data、/admin/data/stocks、/admin/data/etf、/admin/data/etf/history）SHALL 在任务运行中以约 10 秒间隔轮询 summary 与 active run 刷新进度（对齐现行实现 `POLL_MS=10000`）；任务结束后 SHALL 停止高频轮询并保持最终状态（可手动刷新）。轮询数据 SHALL 来自同步小表 API，SHALL NOT 对事实表产生周期性扫描。

#### Scenario: 运行中轮询进度

- **WHEN** 全量个股回填运行中管理员停留在数据管理分区任一页面
- **THEN** 页面每隔约 10 秒更新当前数据集/证券、已处理/成功/失败/跳过计数与 completion_rate，无需手动刷新

#### Scenario: 任务结束停止轮询

- **WHEN** 检测到 active run 消失（run 结束）
- **THEN** 前端停止高频轮询，页面展示最终结果

## ADDED Requirements

### Requirement: ETF 数据管理页面

系统 SHALL 提供管理员页面 `/admin/data/etf`（模板 `admin_data_etf.html` 改写，page_id 与认证依赖、全站两级导航沿用——子导航「ETF数据」active），作为 ETF 数据健康与同步控制台，包含：ETF universe 概况卡（当前 ACTIVE ETF 数、cn_etf_basic 最近成功刷新时间、etf_enabled 状态）、三个数据集状态卡（etf_basic 主档卡：状态/记录数/上次成功刷新/最后错误；etf_daily、etf_adj_factor 数据集卡：与股票数据集卡同构的个股口径统计——证券总数/已追平/落后/今日成功/今日失败/完整度/数据范围/最后成功/最后错误）、当前任务进度与"检查并更新数据"按钮（复用现有 `POST /api/admin/history-data/sync` 202/409 语义与运行中 10 秒轮询）。`history.etf_enabled=false` 时页面 SHALL 显示未启用说明、SHALL NOT 发起 ETF 相关轮询。页面 SHALL 复用现有样式组件与 `api()`/`esc()` 工具，SHALL NOT 引入前端框架。

#### Scenario: 管理员查看 ETF 总览

- **WHEN** 管理员访问 /admin/data/etf 且 ETF 同步已启用
- **THEN** 页面展示 universe 概况、三个数据集卡与同步按钮，导航「ETF数据」active

#### Scenario: 未启用说明

- **WHEN** history.etf_enabled=false 时管理员访问 /admin/data/etf
- **THEN** 页面显示 ETF 模块未启用说明，不展示数据集统计，不发起高频轮询

#### Scenario: 运行中按钮禁用与轮询

- **WHEN** 同步运行中管理员停留在 /admin/data/etf
- **THEN** 按钮禁用显示"正在更新..."，页面按现有节奏轮询进度（当前数据集/ETF 代码、已处理/成功/失败/跳过）

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data/etf
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup）

### Requirement: ETF 个股历史页面

系统 SHALL 提供管理员页面 `/admin/data/etf/history`（模板 `admin_data_etf_history.html` 改写，page_id 与认证依赖、全站两级导航沿用——子导航「ETF历史」active），结构与 `/admin/data/stocks` 同构：数据集切换 chip（etf_daily/etf_adj_factor）、该数据集统计区（总数/已追平/落后/今日成功/今日失败/完整度）、状态筛选（全部/成功/失败）、名称/代码搜索框、服务端分页列表（每页 100 条，默认排序失败优先 → 水位最旧 → ts_code 升序）、失败行只读详情 modal（错误信息经 HTML 转义置于可滚动区域）。列表行 SHALL 展示 ts_code、名称、当前水位（或"未同步"）、最后状态、最后成功时间、最后错误码。页面 SHALL 复用现有 chip/table/status-badge/modal/pagination 组件与 `api()`/`esc()` 工具，SHALL NOT 引入前端框架或构建链，SHALL NOT 提供编辑或重试操作。

#### Scenario: 查看 ETF 列表

- **WHEN** 管理员访问 /admin/data/etf/history 并切换到 etf_adj_factor
- **THEN** 页面展示该数据集统计区与第一页（100 条）ETF 列表，默认排序失败优先

#### Scenario: 搜索与筛选

- **WHEN** 管理员输入"300"并选择"失败"筛选
- **THEN** 列表仅展示代码含"300"且 last_status=failed 的 ETF（服务端过滤），分页元信息随之更新

#### Scenario: 失败详情只读

- **WHEN** 管理员点击某失败行
- **THEN** 打开只读 modal 展示最近失败任务的结构化错误（转义后），SHALL NOT 提供编辑或重试按钮

### Requirement: summary API ETF 扩展

`GET /api/admin/history-data/summary` SHALL 在既有响应基础上新增：`etf_universe` 块（active_count、total_count、last_refreshed_at、enabled——enabled=false 时该块仅含 enabled=false）；`etf_datasets[]` 列表（etf_basic/etf_daily/etf_adj_factor 三个条目：etf_basic 按主档数据集字段结构（状态/记录数/上次成功刷新/最后错误），etf_daily/etf_adj_factor 按日级数据集条目同构 schema——dataset、display_name、status、history_start_date、data_min_date、data_max_date、record_count、last_success_at、last_error_code、last_error 及个股口径统计 stock_count（证券总数，股票与 ETF 条目共用字段名以保持同构）/up_to_date_count/lagging_count/today_success_count/today_failed_count/completion_rate）。`master_datasets` SHALL 包含 etf_basic。`history.etf_enabled=false` 时 SHALL NOT 返回 etf_datasets 与 etf_universe 统计（仅 enabled 标志）。实现 SHALL 只读 history_sync_state、stock_sync_state、cn_etf_basic 等同步小表，SHALL NOT 扫描事实大表。时间字段 SHALL 返回北京时间带时区 ISO 格式。

#### Scenario: 管理员获取 summary 含 ETF 分组

- **WHEN** 管理员 GET /api/admin/history-data/summary 且 ETF 启用
- **THEN** 返回 200 与 etf_universe 块及三个 etf_datasets 条目（个股口径统计与股票数据集字段同构）

#### Scenario: 未启用不返回 ETF 统计

- **WHEN** history.etf_enabled=false 时请求 summary
- **THEN** 响应不含 etf_datasets 统计与 universe 计数，etf_universe.enabled=false，其余字段与 v0.4.1 兼容

#### Scenario: ETF 页面数据来自小表

- **WHEN** summary 含 1000 只 ETF × 2 数据集的统计
- **THEN** 查询只对 stock_sync_state/cn_etf_basic 小表聚合完成，无事实表扫描
