## ADDED Requirements

### Requirement: 数据管理页面

系统 SHALL 提供管理员页面 `/admin/data`（模板 `admin_data.html`，复用现有管理员页面认证依赖与 CSRF meta），作为历史数据健康与同步控制台，包含：顶部总体状态卡（整体状态、数据起点 2010-01-01、最新市场交易日、当前任务、最后执行）、四个日级数据集状态卡（daily/adj_factor/daily_basic/moneyflow：状态、数据范围、连续水位、当前目标、落后交易日数、记录数、当前处理日期、当前 attempt、最后成功、最后错误）、主档状态表（stock_basic/trade_cal/namechange/stock_company：状态、记录数、上次成功刷新、bootstrap/cursor、最后错误，SHALL NOT 显示伪造的交易日水位）、当前任务进度与最近 20 次执行记录（开始时间、触发方式、整体结果、耗时、各数据集推进、错误摘要）。主操作 SHALL 为单一"检查并更新数据"按钮（运行中显示"正在更新..."并禁用），SHALL NOT 要求管理员区分"补缺口/增量/重同步"模式，SHALL NOT 提供历史数据手工编辑或水位输入入口。页面 SHALL 加入现有管理员导航，保持现有 UI 风格与原生 JS（无新前端构建系统）。

#### Scenario: 管理员查看数据状态

- **WHEN** 管理员访问 /admin/data
- **THEN** 页面展示总体状态、四个日级数据集卡、主档状态、当前任务与最近执行记录

#### Scenario: 运行中按钮禁用

- **WHEN** 同步任务运行中管理员打开 /admin/data
- **THEN** 按钮显示"正在更新..."且禁用，页面展示当前数据集/日期/attempt 进度

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup），不展示数据（401 语义仅适用于 /api/admin/history-data/* API）

### Requirement: summary API

`GET /api/admin/history-data/summary` SHALL 仅要求管理员，返回 overall_status、history_start_date、latest_market_trade_date、active_run（或 null）及 daily_datasets/master_datasets 列表；每个日级数据集 SHALL 包含 dataset、display_name、status、history_start_date、data_min_date、data_max_date、latest_complete_trade_date、latest_expected_trade_date、next_trade_date、lag_trade_days、record_count、current_trade_date、current_attempt、last_success_at、last_error_code、last_error。实现 SHALL 只读 history_sync_state 等同步小表，SHALL NOT 扫描事实大表。时间字段 SHALL 返回北京时间带时区 ISO 格式。

#### Scenario: 管理员获取 summary

- **WHEN** 管理员 GET /api/admin/history-data/summary
- **THEN** 返回 200 与上述结构；普通用户 403、未登录 401

#### Scenario: 失败数据集可见

- **WHEN** moneyflow 处于 FAILED（failed_trade_date=2026-09-11、attempt 10/10）
- **THEN** summary 中该数据集展示状态、水位 2026-09-10（失败日的上一交易日，水位不越过失败日）、下一处理日期 2026-09-11、最近尝试与最后错误

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

`GET /api/admin/history-data/runs?limit=20` SHALL 仅要求管理员，返回最近同步执行列表；`GET /api/admin/history-data/runs/{run_id}` SHALL 返回该 run 及每个 dataset 的执行详情（供页面轮询当前进度）。本阶段 SHALL NOT 新增公开的历史数据查询 API（如 /api/history/kline）。

#### Scenario: 查询运行详情

- **WHEN** 管理员 GET /api/admin/history-data/runs/{active_run_id}
- **THEN** 返回 run 状态、trigger、时间与各 dataset 的 start/end watermark、dates_completed、rows_written、retry_count 等进度字段

### Requirement: overall_status 计算

管理员页面与 summary 的 overall_status SHALL 动态计算，优先级：有 active run → RUNNING；任意核心日级数据集 FAILED → ERROR；无 FAILED 但任意核心数据集水位落后目标 → LAGGING；仅存在 WAITING_SOURCE 且未超合理发布时间 → WAITING；四个核心数据集全部 CAUGHT_UP → HEALTHY。stock_company/namechange 短暂失败 SHALL 以 warning 呈现但不必然升级整体 ERROR；trade_cal/stock_basic 失败 SHALL 升级整体异常级别。

#### Scenario: 资金流失败整体异常

- **WHEN** moneyflow FAILED 而其余追平
- **THEN** overall_status 为 ERROR，页面同时显示其余数据集正常

#### Scenario: 全部追平

- **WHEN** 四个日级数据集均 CAUGHT_UP 且无 active run
- **THEN** overall_status 为 HEALTHY

### Requirement: 前端轮询

`/admin/data` 页面 SHALL 在任务运行中每 3~5 秒轮询 summary 与 active run 刷新进度；任务结束后 SHALL 停止高频轮询并保持最终状态（可手动刷新）。轮询数据 SHALL 来自同步小表 API，SHALL NOT 对事实表产生周期性扫描。

#### Scenario: 运行中轮询进度

- **WHEN** 首次回填运行中管理员停留在 /admin/data
- **THEN** 页面每隔数秒更新当前数据集/交易日/attempt/本次写入数，无需手动刷新

#### Scenario: 任务结束停止轮询

- **WHEN** 检测到 active run 消失（run 结束）
- **THEN** 前端停止高频轮询，页面展示最终结果
