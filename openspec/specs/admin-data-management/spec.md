# admin-data-management Specification

## Purpose
TBD - created by archiving change a-share-historical-data. Update Purpose after archive.
## Requirements
### Requirement: 数据管理页面

系统 SHALL 提供管理员页面 `/admin/data`（模板 `admin_data.html`，复用现有管理员页面认证依赖与 CSRF meta），作为历史数据健康与同步控制台，包含：顶部总体状态卡（整体状态、数据起点 2010-01-01、最新市场交易日、当前任务、最后执行）、四个日级数据集状态卡（daily/adj_factor/daily_basic/moneyflow：状态、数据范围、记录数、股票总数、已追平股票数、落后股票数、完整度（已追平占比）、今日成功/今日失败、最后成功、最后错误——个股口径，SHALL NOT 展示退役的日级连续水位/当前处理日期/当前 attempt 运营字段）、主档状态表（stock_basic/trade_cal/namechange/stock_company：状态、记录数、上次成功刷新、bootstrap/cursor、最后错误，SHALL NOT 显示伪造的交易日水位）、当前任务进度（当前数据集/股票、已处理/成功/失败/跳过）与最近 20 次执行记录（开始时间、触发方式、整体结果、耗时、各数据集统计、错误摘要）。主操作 SHALL 为单一"检查并更新数据"按钮（运行中显示"正在更新..."并禁用），SHALL NOT 要求管理员区分"补缺口/增量/重同步"模式，SHALL NOT 提供历史数据手工编辑或水位输入入口。页面 SHALL 继续加入现有管理员导航并提供通往 `/admin/data/stocks` 个股历史页面的入口，保持现有 UI 风格与原生 JS（无新前端构建系统）。

#### Scenario: 管理员查看数据状态

- **WHEN** 管理员访问 /admin/data
- **THEN** 页面展示总体状态、四个日级数据集个股口径卡片、主档状态、当前任务与最近执行记录

#### Scenario: 运行中按钮禁用

- **WHEN** 同步任务运行中管理员打开 /admin/data
- **THEN** 按钮显示"正在更新..."且禁用，页面展示当前数据集/股票与已处理/成功/失败/跳过进度

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup），不展示数据（401 语义仅适用于 /api/admin/history-data/* API）

### Requirement: summary API

`GET /api/admin/history-data/summary` SHALL 仅要求管理员，返回 overall_status、history_start_date、latest_market_trade_date、active_run（或 null）及 daily_datasets/master_datasets 列表；每个日级数据集 SHALL 包含 dataset、display_name、status（由个股口径派生：存在落后股票 → LAGGING、全部追平 → CAUGHT_UP、本轮系统级失败 → FAILED）、history_start_date、data_min_date、data_max_date、record_count、last_success_at、last_error_code、last_error（数据集段处理完成时刷新，维护义务见 historical-data-sync"同步执行记录"），以及个股口径统计：stock_count（universe 股票总数）、up_to_date_count、lagging_count、today_success_count、today_failed_count、completion_rate（up_to_date_count/stock_count）。旧水位字段（latest_complete_trade_date、latest_expected_trade_date、next_trade_date、lag_trade_days、current_trade_date、current_attempt）SHALL 保留输出冻结值以兼容历史展示，SHALL NOT 作为个股同步运营口径。实现 SHALL 只读 history_sync_state、stock_sync_state 等同步小表（个股统计为小表聚合），SHALL NOT 扫描事实大表。时间字段 SHALL 返回北京时间带时区 ISO 格式。

#### Scenario: 管理员获取 summary

- **WHEN** 管理员 GET /api/admin/history-data/summary
- **THEN** 返回 200 与上述结构；普通用户 403、未登录 401

#### Scenario: 个股统计口径正确

- **WHEN** 某数据集 universe 6000 只、5990 只追平、10 只落后（其中 3 只今日失败）
- **THEN** summary 中该数据集 stock_count=6000、up_to_date_count=5990、lagging_count=10、today_failed_count=3、completion_rate≈99.83%

### Requirement: 手动同步 API

`POST /api/admin/history-data/sync` SHALL 仅要求管理员且校验现有 CSRF 机制（X-CSRF-Token），成功启动返回 202 与 {run_id, status:"RUNNING"}；已有任务运行时返回 409 与当前 run_id 及"历史数据同步正在运行"信息；HTTP 请求 SHALL NOT 等待同步完成。requested_by_user_id SHALL 服务端从当前认证用户取得，SHALL NOT 接受客户端传入 user_id。普通用户 SHALL 403、未登录 401、CSRF 无效 SHALL 拒绝。

#### Scenario: 触发成功

- **WHEN** 管理员携带有效 CSRF POST /api/admin/history-data/sync 且无运行中任务
- **THEN** 返回 202 与新 run_id，后台开始执行统一同步逻辑

#### Scenario: 运行中 409

- **WHEN** 任务运行中重复 POST
- **THEN** 返回 409 与正在运行的 run_id，不启动第二个任务

#### Scenario: CSRF 缺失拒绝

- **WHEN** 管理员 POST 未携带有效 X-CSRF-Token
- **THEN** 请求被拒绝，不触发同步

### Requirement: 执行记录 API

`GET /api/admin/history-data/runs?limit=20` SHALL 仅要求管理员，返回最近同步执行列表；`GET /api/admin/history-data/runs/{run_id}` SHALL 返回该 run 及每个 dataset 的执行详情（供页面轮询当前进度）：各数据集 SHALL 返回 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count` 与 `rows_written`/`request_count`/`retry_count`；旧水位列（start/end watermark、target_trade_date、dates_completed、failed_trade_date）SHALL 作为冻结兼容字段输出（新产生的 run 恒为 NULL/0）。运行中 run 的实时进度（当前 dataset/ts_code、已处理/成功/失败/跳过）SHALL 一并返回（来源为进程内进度快照，聚合计数以数据库为准）。本阶段 SHALL NOT 新增公开的历史数据查询 API（如 /api/history/kline）。

#### Scenario: 查询运行详情

- **WHEN** 管理员 GET /api/admin/history-data/runs/{active_run_id}
- **THEN** 返回 run 状态、trigger、时间与各 dataset 的 processed_count/task_success_count/task_failed_count/skipped_count、rows_written、retry_count 及运行中实时进度

#### Scenario: 旧水位列为兼容输出

- **WHEN** 查看迁移后新产生 run 的 dataset 详情
- **THEN** start/end watermark、dates_completed 输出 NULL/0（冻结兼容），页面"各数据集推进"以新统计列口径展示

### Requirement: overall_status 计算

管理员页面与 summary 的 overall_status SHALL 动态计算，优先级：有 active run → RUNNING；上次 Run 系统级 FAILED → ERROR；无 FAILED 但任一核心日级数据集存在落后（lagging）股票 → LAGGING（"存在缺口"；个股失败 SHALL NOT 单独升级整体 ERROR）；四个核心数据集全部股票 up_to_date 且无 active run → HEALTHY。stock_company/namechange 短暂失败 SHALL 以 warning 呈现但不必然升级整体 ERROR；trade_cal/stock_basic 失败 SHALL 升级整体异常级别。

#### Scenario: 个股失败不升级整体异常

- **WHEN** moneyflow 有 2 只股票 task failed 而其余全部追平
- **THEN** overall_status 为 LAGGING（数据集 lagging_count=2），不为 ERROR，页面同时显示整体正常与缺口明细

#### Scenario: 全部追平

- **WHEN** 四个日级数据集全部股票 up_to_date 且无 active run
- **THEN** overall_status 为 HEALTHY

### Requirement: 前端轮询

`/admin/data` 页面 SHALL 在任务运行中每 3~5 秒轮询 summary 与 active run 刷新进度；任务结束后 SHALL 停止高频轮询并保持最终状态（可手动刷新）。轮询数据 SHALL 来自同步小表 API，SHALL NOT 对事实表产生周期性扫描。

#### Scenario: 运行中轮询进度

- **WHEN** 全量个股回填运行中管理员停留在 /admin/data
- **THEN** 页面每隔数秒更新当前数据集/股票、已处理/成功/失败/跳过计数与 completion_rate，无需手动刷新

#### Scenario: 任务结束停止轮询

- **WHEN** 检测到 active run 消失（run 结束）
- **THEN** 前端停止高频轮询，页面展示最终结果

### Requirement: 个股历史页面

系统 SHALL 提供管理员页面 `/admin/data/stocks`（模板 `admin_data_stocks.html`，复用现有管理员认证依赖、CSRF meta 与 admin 布局），包含：数据集切换（daily/adj_factor/daily_basic/moneyflow）、该数据集个股统计区（股票总数/已追平/落后/今日成功/今日失败/完整度）、状态筛选（全部/成功/失败）、名称/代码搜索框、分页列表（服务端分页，每页 100 条）与失败详情只读 modal。列表默认排序 SHALL 为"失败优先 → 水位最旧优先 → ts_code 升序"；行展示 ts_code、名称、当前水位（或"未同步"）、最后状态、最后成功时间、最后错误码。失败行 SHALL 可点击打开只读 modal 展示最近任务详情（错误码/错误信息/重试次数/区间），modal 中错误信息 SHALL 经 HTML 转义置于可滚动区域。页面 SHALL 复用现有样式组件（chip、table、status-badge、modal 模式）与 `api()`/`esc()` 工具，SHALL NOT 引入前端框架或构建链。

#### Scenario: 查看个股列表

- **WHEN** 管理员访问 /admin/data/stocks 并切换到 moneyflow
- **THEN** 页面展示该数据集统计区与第一页（100 条）个股列表，默认排序失败优先

#### Scenario: 搜索与筛选

- **WHEN** 管理员输入"招商"并选择"失败"筛选
- **THEN** 列表仅展示名称含"招商"且 last_status=failed 的股票（服务端过滤），分页元信息随之更新

#### Scenario: 失败详情只读

- **WHEN** 管理员点击某失败行
- **THEN** 打开只读 modal 展示最近失败任务的结构化错误（转义后），SHALL NOT 提供编辑或重试按钮

### Requirement: 个股列表 API

`GET /api/admin/history-data/stocks?dataset=&status=all|success|failed&q=&page=` SHALL 仅要求管理员：dataset 必填且仅接受四个日级数据集名；page_size SHALL 服务端固定为 100（客户端传入更大值被钳制）；查询 SHALL 为 `cn_stock_basic LEFT JOIN stock_sync_state`（按 dataset 过滤），状态筛选与名称/代码搜索（LIKE）SHALL 在 SQL 内完成，SHALL NOT 将全量股票加载到浏览器。默认排序 SHALL 为 `last_status='failed' 优先 → watermark_date 升序（NULL 最前）→ ts_code 升序`。响应 SHALL 包含分页列表（ts_code、名称、watermark_date、last_status、last_success_at、last_error_code、last_task_id——供失败行直连任务详情 API）、分页元信息（总数、当前页、总页数）及该数据集统计块（与 summary 同口径）。

#### Scenario: 分页与排序

- **WHEN** 管理员请求 dataset=daily 无筛选的第 1 页
- **THEN** 返回按默认排序的前 100 条与分页元信息；page=2 返回后续 100 条

#### Scenario: 无效参数拒绝

- **WHEN** 请求 dataset=trade_cal 或 page=0
- **THEN** 返回 422，不产生查询

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户请求该 API
- **THEN** 普通用户 403、未登录 401

### Requirement: 任务详情 API

`GET /api/admin/history-data/tasks/{task_id}` SHALL 仅要求管理员，按 id 直查 `sync_task` 并 JOIN 主档补充证券名称，返回任务完整详情（dataset、ts_code、instrument_id、run_id、start_date/end_date、status、retry_count/attempt_count、records_fetched/records_written、error_code/error_type/error_message、started_at/finished_at/duration_ms）；task_id 不存在 SHALL 返回 404。错误文本 SHALL 已脱敏（不含 Token）。本 API 为只读，SHALL NOT 提供任务重试或状态修改操作。

#### Scenario: 查询失败任务详情

- **WHEN** 管理员 GET /api/admin/history-data/tasks/{id} 且该任务为 failed
- **THEN** 返回 200 与完整结构化错误信息（含 error_code 与脱敏 error_message）

#### Scenario: 任务不存在

- **WHEN** 管理员 GET 不存在的 task_id
- **THEN** 返回 404

