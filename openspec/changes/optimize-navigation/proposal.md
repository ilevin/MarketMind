## Why

v0.4.0 的页面导航由 8 个登录态模板各自内联维护：`header.topbar` + `nav.user-nav` 在各模板间逐字复制并各自微调（如 `app/templates/admin_data_stocks.html` 与 `admin_users.html` 链接集合、顺序、文案均不同），无共享模板、无当前位置（active）标识，且已出现明显不一致——管理页不提供 `/watchlist`、`/tags` 入口，`change_password.html` 缺自选/标签入口，`admin_status.html` 缺 CSRF meta，各页 `<title>` 与顶栏 h1 混用「站点名/页面名」两种口径。用户在任意页面无法感知所处分区，也无法一步跳转到其他分区。

设计稿 `../temp/MarketMind_navigation_design_v2.html` 已确定两级导航方案：顶部主导航（行情首页/数据管理/系统设置）+ 分区子导航 + 右侧用户菜单，其中数据管理分区规划了「ETF数据」「ETF历史」两个页面。本变更先把导航骨架按设计稿立起来，ETF 页面以占位形式就位，为后续 ETF 数据能力铺路。

## What Changes

- **共享导航模板**：新增 `app/templates/base.html`，全部 8 个登录态页面模板改为 `{% extends %}` 复用，消除逐页复制粘贴；`/login`、`/setup` 匿名页保持独立无导航。
- **两级导航**：实现顶部主导航三分区（行情首页、数据管理、系统设置）+ 子导航条目（行情/自选管理/标签管理；股票数据/个股历史/ETF数据/ETF历史；用户管理/系统状态）+ 右侧用户菜单；顶栏按设计稿改为深色样式。
- **当前位置标识**：主导航项与子导航条目增加 active 状态（视觉高亮 + `aria-current`），替代现状「本页链接不出现」的隐式做法。
- **角色可见性**：「数据管理」「系统设置」两个主导航分区（含各自子导航）仅管理员可见；普通用户主导航仅「行情首页」。
- **用户菜单**：顶栏右侧用户名按钮 hover 下拉，含「修改密码」「退出登录」；退出登录行为不变（沿用 `#logout-link` 与 `bindLogout()`）。
- **ETF 占位页面**：新增 `/admin/data/etf`（ETF数据）与 `/admin/data/etf/history`（ETF历史）两个管理员占位页面，仅渲染空状态说明，不提供任何数据操作；ETF 数据同步本身不在本变更范围。
- **一致化修复**：统一 `<title>` 为「页面名 · MarketMind」，顶栏 logo 统一为「📈 MarketMind」，`admin_status.html` 补齐 CSRF meta。
- **保留不变**：各页面内容区结构与功能、前端轮询策略（admin-data 运行中 10 秒轮询等）、认证与会话机制、全部 `/api/*` 契约、DuckDB 无变更。
- **明确不做**（非目标）：不实现 ETF 数据同步或展示；不引入前端框架或构建链；不做移动端响应式适配（维持桌面优先）；不改 `/login`、`/setup` 匿名页。无 BREAKING 变更（纯模板/样式/新增只读页面）。

## Capabilities

### New Capabilities
- `site-navigation`: 全站两级导航系统——主导航分区与子导航条目定义、当前位置 active 标识、分区角色可见性、用户菜单（用户名展示、「修改密码」「退出登录」入口与登出行为），适用于全部登录态页面。

### Modified Capabilities
- `dashboard-ui`: 移除『导航栏用户信息』requirement，其规范内容（用户名展示、修改密码/退出登录入口、登出撤销 Session 跳转 `/login`）由 `site-navigation` 的『用户菜单』requirement 按新结构承载。
- `admin-data-management`: 『数据管理页面』的导航条款由「继续加入现有管理员导航」改为采用全站两级导航（数据管理分区子导航含 ETF 入口）；新增『ETF 数据占位页面』『ETF 历史占位页面』两个 requirement。

## Impact

- **模板**：`app/templates/` 新增 `base.html`、`admin_data_etf.html`、`admin_data_etf_history.html`；重写 `index.html`、`watchlist.html`、`tags.html`、`change_password.html`、`admin_users.html`、`admin_data.html`、`admin_data_stocks.html`、`admin_status.html`（去内联导航、extends base、声明所属分区）；`login.html`、`setup.html` 不变。
- **路由**：`app/main.py` 新增两个 GET 页面路由（`/admin/data/etf`、`/admin/data/etf/history`，`require_admin_page`），既有路由不动。
- **样式**：`app/static/style.css` 重定义 `.topbar`（深色）并新增 subbar、nav/sub-item active、user-menu 下拉样式（设计稿色板）；内容区样式（`.panel`、`.table` 等）不动。
- **JS**：`app/static/app.js` 预期不变（`bindLogout()` 与 `#logout-link` 兼容；ETF 占位页无需 init 函数）。
- **测试**：`tests/integration/test_admin_data_page.py`（内联导航断言：本页不重复链接、管理员每页可见数据管理入口、个股页返回链接）、`tests/integration/test_auth_permissions.py`（/admin/status 管理员导航入口断言）需按新导航更新；新增导航结构集成测试（分区/active/角色可见性/ETF 占位页鉴权）。
- **文档与版本**：`docs/CHANGELOG.md` 新增 v0.5.0 条目；`docs/README.md` 页面清单补 ETF 占位页；`app/version.py` 升 v0.5.0。
- **回滚**：纯模板/样式/新增只读页面变更，git revert 单提交回滚，无数据迁移与配置变更。
