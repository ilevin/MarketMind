## 1. 导航样式与共享基础模板

- [x] 1.1 `app/static/style.css`：重定义 `.topbar`（深色 `#0f172a`、64px、flex 布局），新增 `.logo`、`.nav-main`、`.nav-item`（含 `.active` 与 hover，背景 `#1e293b` 白字）、`.user`、`.user-btn`、`.user-menu`（`:hover` 纯 CSS 下拉）、`.subbar`（白底 52px、底边框 `#e5e7eb`）、`.sub-item`（pill 圆角，`.active` 背景 `#dbeafe` 文字 `#2563eb`）与 `.page-heading`（内容区页面标题）样式；现有 `.container`/`.panel`/`.table` 等内容区样式不动
- [x] 1.2 新增 `app/templates/base.html`：`<head>` 统一承载 title 块、`<meta name="csrf-token" content="{{ current_user.csrf_token }}">`、`/static/style.css?v={{ app_version }}` 引用；顶栏含 logo「📈 MarketMind」、主导航三分区（「数据管理」「系统设置」以 `{% if current_user.is_admin %}` 整体包裹）与右侧用户菜单（`#logout-link`，沿用 `bindLogout()`）；子导航栏按 `nav_section` 渲染当前分区条目；`NAV` 分区定义表内置于模板（`market`/`data`/`setting` → 条目与 href，见 design D3），active 以 `nav_item` 匹配并输出 `aria-current`；内容区预留 content 块与 footer 块；顶部注释固化「子模板先 `{% set nav_section/nav_item %}` 再 `{% extends %}`」约定
- [x] 1.3 `app/static/app.js` 确认无需改动：`bindLogout()` 绑定 `#logout-link`、`body[data-page]` 分发与各页轮询逻辑全部原样（下拉与 active 均无 JS 参与）

## 2. 业务分区页面改造（market 分区）

- [x] 2.1 `app/templates/index.html` 改为 extends base（`nav_section="market"`、`nav_item="行情"`）：移除内联 topbar/user-nav，内容区（市场状态、指数卡、自选表格、标签筛选 `#tag-filter`）不动；保留 `.footer` 版本号页脚（经 base footer 块或页内保留）；`<title>` 改「行情首页 · MarketMind」，内容区顶部补 `<h1 class="page-heading">`
- [x] 2.2 `app/templates/watchlist.html` 改为 extends base（`nav_item="自选管理"`）：移除内联导航，内容区两 panel 与标签编辑弹层（`#tag-edit-modal` 等 id/class）不动，title 改「自选管理 · MarketMind」
- [x] 2.3 `app/templates/tags.html` 改为 extends base（`nav_item="标签管理"`）：移除内联导航，`#add-tag-form`/`#tags-table` 不动，title 改「标签管理 · MarketMind」

## 3. 管理分区与无分区页面改造

- [x] 3.1 `app/templates/admin_data.html` 改为 extends base（`nav_section="data"`、`nav_item="股票数据"`）：移除内联导航与「个股历史」手写链接（由子导航取代），五个内容区块（总览/日级数据集/主档/任务进度/执行记录）与 `data-page="admin-data"` 不动
- [x] 3.2 `app/templates/admin_data_stocks.html` 改为 extends base（`nav_item="个股历史"`）：删除「← 数据总览」返回链接（子导航取代），chip/筛选/表格/分页/`#error-modal` 不动，title 改「个股历史 · MarketMind」
- [x] 3.3 `app/templates/admin_users.html` 改为 extends base（`nav_section="setting"`、`nav_item="用户管理"`）：移除内联导航，`#create-user-form`/`#users-table` 不动
- [x] 3.4 `app/templates/admin_status.html` 改为 extends base（`nav_item="系统状态"`）：移除内联导航，jobs/providers 服务端渲染表格不动；CSRF meta 随 base 统一注入（修复本页缺失）
- [x] 3.5 `app/templates/change_password.html` 改为 extends base（无分区：`nav_section` 不设或设为 none）：顶栏渲染但主导航无 active、不渲染子导航栏，`#change-password-form` 不动
- [x] 3.6 确认 `app/templates/login.html`、`setup.html` 零改动（不 extends base，匿名页无导航）

## 4. ETF 占位页面

- [x] 4.1 `app/main.py` 新增 `GET /admin/data/etf` 与 `GET /admin/data/etf/history` 两个页面路由（`require_admin_page`，上下文 `current_user`），分别渲染 `admin_data_etf.html`、`admin_data_etf_history.html`
- [x] 4.2 新增 `app/templates/admin_data_etf.html` 与 `app/templates/admin_data_etf_history.html`：extends base（`nav_section="data"`、`nav_item="ETF数据"`/`"ETF历史"`），`data-page="admin-data-etf"`/`"admin-data-etf-history"`（无 init 函数，预留），内容区单个 `.panel`「敬请期待」空状态说明，无任何数据操作

## 5. 测试

- [x] 5.1 更新 `tests/integration/test_admin_data_page.py` 既有导航断言：「管理员在每个页面都能看到数据管理入口」（现按 `/admin/data` href 断言，改为按新主导航/子导航结构断言）与个股页「返回数据总览链接」（删除或改为断言子导航含 `/admin/data/stocks` 入口）；页面内容断言不动
- [x] 5.2 更新 `tests/integration/test_auth_permissions.py` 中 /admin/status 管理员导航入口相关断言，按新导航结构调整
- [x] 5.3 新增导航集成测试（建议 `tests/integration/test_navigation.py`）：管理员访问 `/`、`/watchlist`、`/admin/data`、`/admin/users` 断言主导航三分区与子导航条目/链接（含 ETF 两入口）、active/`aria-current` 位置正确、跨页面导航一致；普通用户断言仅见「行情首页」分区、无 `/admin/*` 入口；`/change-password` 断言无子导航栏、主导航无 active；用户菜单断言用户名/管理员标注/修改密码/退出登录
- [x] 5.4 新增 ETF 占位页测试：管理员访问两页 200 且含「敬请期待」与对应子导航 active；普通用户 403、未登录按初始化状态 302（并入 5.3 或单独文件）
- [x] 5.5 全量离线回归：`.venv/bin/python -m pytest -m "not online" -q` 全绿

## 6. 文档、版本与发布

- [x] 6.1 `docs/CHANGELOG.md` 新增 v0.5.0 条目：两级导航（共享 base.html、active 标识、角色可见性、用户菜单）、ETF 占位页、一致化修复（title/CSRF meta）
- [x] 6.2 `docs/README.md` 页面清单补充 `/admin/data/etf`、`/admin/data/etf/history` 占位页与导航结构说明；`config.example.yaml` 无新增配置项，确认无需变更
- [x] 6.3 `app/version.py` 升 `APP_VERSION = "v0.5.0"`；同步 `pyproject.toml`（`version = "0.5.0"`）与 `Dockerfile` 离线构建 pin（`marketmind==0.5.0`）
- [x] 6.4 冒烟（脚本化执行：/tmp/mm_smoke/smoke_navigation.py，TestClient 真实登录全流程核对 10 页导航/active/用户菜单/登出/角色可见性/匿名跳转；hover 为纯 CSS 经代码复核）：登录后逐页（`/`、`/watchlist`、`/tags`、`/change-password`、`/admin/data`、`/admin/data/stocks`、`/admin/data/etf`、`/admin/data/etf/history`、`/admin/users`、`/admin/status`）核对导航结构、active 位置、用户菜单 hover/登出；以普通用户核对分区不可见
- [x] 6.5 提交变更（单提交，便于 revert 回滚）
