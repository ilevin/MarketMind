# site-navigation Specification

## Purpose
TBD - created by archiving change optimize-navigation. Update Purpose after archive.
## Requirements
### Requirement: 两级导航结构

全部登录态页面（行情、自选管理、标签管理、修改密码、数据管理分区各页、系统设置分区各页）SHALL 采用统一的两级导航：顶部主导航栏含站点 logo、主导航分区项与右侧用户菜单；主导航栏下方 SHALL 展示当前分区的子导航栏（分区条目集合见『主导航分区与子导航条目』）。`/login`、`/setup` 匿名页 SHALL NOT 渲染导航。无分区页面（修改密码）SHALL 渲染顶部主导航栏但 SHALL NOT 渲染子导航栏。

#### Scenario: 业务页两级导航

- **WHEN** 登录用户访问 `/watchlist`
- **THEN** 页面顶栏显示 logo、主导航（「行情首页」为当前分区）与用户菜单，下方子导航显示「行情」「自选管理」「标签管理」三个条目

#### Scenario: 管理页两级导航

- **WHEN** 管理员访问 `/admin/data`
- **THEN** 页面顶栏显示主导航（「数据管理」为当前分区），子导航显示「股票数据」「个股历史」「ETF数据」「ETF历史」四个条目

#### Scenario: 匿名页无导航

- **WHEN** 未登录用户访问 `/login`
- **THEN** 页面仅含登录表单，不含主导航栏与子导航栏

#### Scenario: 无分区页面

- **WHEN** 登录用户访问 `/change-password`
- **THEN** 页面渲染顶栏（无任何主导航项处于 active 状态）与用户菜单，不渲染子导航栏

### Requirement: 主导航分区与子导航条目

主导航 SHALL 含三个分区，子导航条目与链接 SHALL 固定为：「行情首页」分区——行情（`/`）、自选管理（`/watchlist`）、标签管理（`/tags`）；「数据管理」分区——股票数据（`/admin/data`）、个股历史（`/admin/data/stocks`）、ETF数据（`/admin/data/etf`）、ETF历史（`/admin/data/etf/history`）；「系统设置」分区——用户管理（`/admin/users`）、系统状态（`/admin/status`）。「数据管理」与「系统设置」分区 SHALL 仅对 role=admin 用户可见（主导航项与对应子导航整体不渲染）；普通用户主导航 SHALL 仅显示「行情首页」分区。分区归属 SHALL NOT 因页面而异（业务页与管理页导航一致）。

#### Scenario: 普通用户主导航

- **WHEN** role=user 登录后访问任意登录态页面
- **THEN** 主导航仅显示「行情首页」，不出现「数据管理」「系统设置」分区及其子导航条目

#### Scenario: 管理员跨分区可达

- **WHEN** 管理员访问 `/admin/status`
- **THEN** 主导航显示三个分区，子导航显示「用户管理」「系统状态」；顶栏可一步进入各分区首页（`/`、`/admin/data`、`/admin/users`），分区其余条目（`/watchlist`、`/tags` 等）经对应分区子导航可达

### Requirement: 当前位置标识

导航 SHALL 标识当前位置：当前页面所属的主导航分区项与对应子导航条目 SHALL 处于 active 状态（视觉高亮样式并携带 `aria-current` 属性），其余导航项 SHALL NOT。无分区页面（修改密码）主导航 SHALL 无 active 项。用户菜单与 logo SHALL NOT 参与 active 标识。

#### Scenario: 管理子页 active

- **WHEN** 管理员访问 `/admin/data/etf`
- **THEN** 主导航「数据管理」与子导航「ETF数据」均处于 active 状态，其余导航项不处于 active 状态

#### Scenario: 业务页 active

- **WHEN** 登录用户访问 `/tags`
- **THEN** 主导航「行情首页」与子导航「标签管理」均处于 active 状态，其余导航项不处于 active 状态

### Requirement: 用户菜单

顶栏右侧 SHALL 提供用户菜单：按钮显示当前登录用户名（管理员附加「管理员」标注，普通用户无附加标注），hover 或键盘聚焦（`focus-within`）展开下拉菜单，含「修改密码」（链接 `/change-password`）与「退出登录」入口。退出登录 SHALL 调用 logout API（POST `/api/auth/logout`）并跳转 `/login`，回退按钮 SHALL NOT 恢复登录态。

#### Scenario: 普通用户菜单

- **WHEN** role=user 登录后查看任意登录态页面顶栏
- **THEN** 用户菜单按钮显示其用户名（无「管理员」标注），下拉菜单含「修改密码」与「退出登录」

#### Scenario: 管理员菜单

- **WHEN** role=admin 登录后查看任意登录态页面顶栏
- **THEN** 用户菜单按钮显示其用户名与「管理员」标注，下拉菜单含「修改密码」与「退出登录」

#### Scenario: 退出登录

- **WHEN** 点击用户菜单中的「退出登录」
- **THEN** Session 撤销并跳转 `/login`，回退按钮无法恢复登录态

### Requirement: 导航渲染一致性

全部登录态页面的导航 SHALL 由共享导航模板统一渲染，同一用户在不同页面看到的分区集合、条目集合与链接 SHALL 一致；新增登录态页面 SHALL 声明所属主导航分区（或明确为无分区页面）后纳入该导航结构。

#### Scenario: 跨页面导航一致

- **WHEN** 管理员依次访问 `/`、`/admin/data`、`/admin/users`
- **THEN** 三页主导航的分区、条目与链接完全一致，仅 active 状态不同

