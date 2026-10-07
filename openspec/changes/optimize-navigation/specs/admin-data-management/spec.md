## MODIFIED Requirements

### Requirement: 数据管理页面

系统 SHALL 提供管理员页面 `/admin/data`（模板 `admin_data.html`，复用现有管理员页面认证依赖与 CSRF meta），作为历史数据健康与同步控制台，包含：顶部总体状态卡（整体状态、数据起点 2010-01-01、最新市场交易日、当前任务、最后执行）、四个日级数据集状态卡（daily/adj_factor/daily_basic/moneyflow：状态、数据范围、记录数、股票总数、已追平股票数、落后股票数、完整度（已追平占比）、今日成功/今日失败、最后成功、最后错误——个股口径，SHALL NOT 展示退役的日级连续水位/当前处理日期/当前 attempt 运营字段）、主档状态表（stock_basic/trade_cal/namechange/stock_company：状态、记录数、上次成功刷新、bootstrap/cursor、最后错误，SHALL NOT 显示伪造的交易日水位）、当前任务进度（当前数据集/股票、已处理/成功/失败/跳过）与最近 20 次执行记录（开始时间、触发方式、整体结果、耗时、各数据集统计、错误摘要）。主操作 SHALL 为单一"检查并更新数据"按钮（运行中显示"正在更新..."并禁用），SHALL NOT 要求管理员区分"补缺口/增量/重同步"模式，SHALL NOT 提供历史数据手工编辑或水位输入入口。页面 SHALL 采用全站两级导航（见 `site-navigation` 规格）：主导航当前分区为「数据管理」，子导航 SHALL 提供通往 `/admin/data/stocks`（个股历史）、`/admin/data/etf`（ETF数据）与 `/admin/data/etf/history`（ETF历史）页面的入口，保持现有 UI 风格与原生 JS（无新前端构建系统）。

#### Scenario: 管理员查看数据状态

- **WHEN** 管理员访问 /admin/data
- **THEN** 页面展示总体状态、四个日级数据集个股口径卡片、主档状态、当前任务与最近执行记录

#### Scenario: 运行中按钮禁用

- **WHEN** 同步任务运行中管理员打开 /admin/data
- **THEN** 按钮显示"正在更新..."且禁用，页面展示当前数据集/股票与已处理/成功/失败/跳过进度

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup），不展示数据（401 语义仅适用于 /api/admin/history-data/* API）

## ADDED Requirements

### Requirement: ETF 数据占位页面

系统 SHALL 提供管理员页面 `/admin/data/etf`（模板 `admin_data_etf.html`，复用现有管理员页面认证依赖与全站两级导航，见 `site-navigation` 规格），作为 ETF 数据管理功能的占位页面：内容区仅展示「敬请期待」空状态说明，SHALL NOT 提供任何数据查询、同步或编辑操作。ETF 数据管理能力由后续变更实现。

#### Scenario: 管理员访问占位页

- **WHEN** 管理员访问 /admin/data/etf
- **THEN** 页面经全站两级导航渲染（子导航「ETF数据」处于 active 状态），内容区仅展示占位说明，无任何数据操作

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data/etf
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup）

### Requirement: ETF 历史占位页面

系统 SHALL 提供管理员页面 `/admin/data/etf/history`（模板 `admin_data_etf_history.html`，复用现有管理员页面认证依赖与全站两级导航，见 `site-navigation` 规格），作为 ETF 历史数据功能的占位页面：内容区仅展示「敬请期待」空状态说明，SHALL NOT 提供任何数据查询、同步或编辑操作。ETF 历史数据能力由后续变更实现。

#### Scenario: 管理员访问占位页

- **WHEN** 管理员访问 /admin/data/etf/history
- **THEN** 页面经全站两级导航渲染（子导航「ETF历史」处于 active 状态），内容区仅展示占位说明，无任何数据操作

#### Scenario: 非管理员拒绝

- **WHEN** 普通用户或未登录用户访问 /admin/data/etf/history
- **THEN** 普通用户返回 403；未登录用户按现有管理员页面行为 302 跳转登录页（空库未初始化时跳转 /setup）
