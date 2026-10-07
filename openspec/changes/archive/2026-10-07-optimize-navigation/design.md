## Context

- 现状导航为复制粘贴式：8 个登录态模板各自内联 `header.topbar` + `nav.user-nav`（`app/templates/index.html`、`watchlist.html`、`tags.html`、`change_password.html`、`admin_users.html`、`admin_data.html`、`admin_data_stocks.html`、`admin_status.html`），无任何 `{% extends %}`/`{% include %}` 共享机制；`/login`、`/setup` 匿名页独立无导航。
- 不一致已实际发生：管理页不提供 `/watchlist`、`/tags` 入口；`change_password.html` 缺自选/标签入口；`admin_data_stocks.html` 返回链接指向 `/admin/data` 而其他管理页指向 `/`；各页链接顺序、箭头文案（「→」「←」）不一；`admin_status.html` 是唯一缺 `<meta name="csrf-token">` 的登录页；`<title>` 混用「页面名 · 行情看板」与「站点名」两种口径，顶栏 h1 同样混用。
- 全站无 active/aria-current 状态（唯一隐式约定是「本页链接不出现」）；`app/static/style.css`（365 行，浅色主题、无 CSS 变量、无响应式）现有 `.topbar` 为白底（style.css:12-22），无 subbar、无 active 类、无下拉样式。
- 路由与鉴权（`app/main.py:228-332`）：`/`、`/watchlist`、`/tags`、`/change-password` 用 `require_user_page`；`/admin/users`、`/admin/data`、`/admin/data/stocks`、`/admin/status` 用 `require_admin_page`；各页上下文传 `current_user`（`username`/`role`/`csrf_token` 等，`app/auth/session.py`），全局注入 `app_version`（`app/main.py:195`）。
- JS 机制：`app/static/app.js` 按 `body[data-page]` 分发（app.js:1275-1287），`bindLogout()` 绑定 `#logout-link`（POST `/api/auth/logout` 后跳 `/login`）；无任何下拉/active 切换逻辑。
- 设计稿 `../temp/MarketMind_navigation_design_v2.html`：顶栏 64px 深色 `#0f172a`（logo「📈 MarketMind」+ 主导航 + 右侧用户菜单 hover 下拉）；子导航栏 52px 白底、pill 条目（active 背景 `#dbeafe`、文字 `#2563eb`）；三分区结构——行情首页（行情/自选管理/标签管理）、数据管理（股票数据/个股历史/ETF数据/ETF历史）、系统设置（用户管理/系统状态）。

## Goals / Non-Goals

**Goals:**
- 全部 8 个登录态页面统一为设计稿 v2 的两级导航（主导航分区 + 分区子导航 + 用户菜单），导航单点维护（共享模板）。
- 当前位置可感知：主导航与子导航 active 标识（视觉高亮 + `aria-current`）。
- 角色可见性：数据管理/系统设置分区仅管理员可见；管理员从任意页面可一步到达全部分区。
- ETF 数据/ETF 历史以占位页就位（`/admin/data/etf`、`/admin/data/etf/history`），子导航入口先立起来。
- 既有功能零回归：内容区结构、轮询策略、鉴权语义、`/api/*` 契约不变。

**Non-Goals:**
- 不实现 ETF 数据同步、查询或展示（占位页仅「敬请期待」）。
- 不引入前端框架、构建链、CSS 变量体系（延续「原生 Jinja2 + 原生 JS/CSS」约束）。
- 不做移动端响应式适配（项目维持桌面优先）。
- 不改 `/login`、`/setup` 匿名页。
- 不重构内容区样式（`.panel`/`.table` 等保持），设计稿 `.content`/`.card` 仅作布局示意。

## Decisions

### D1. 导航组件化：Jinja2 `{% extends %}` + 单一 `base.html`

`app/templates/base.html` 承载 `<head>`（title 块、CSRF meta、样式引用）、顶栏（logo + 主导航 + 用户菜单）、子导航栏与内容容器，子模板仅填充内容块。

备选否弃：(a) `{% include %}` 局部宏——每页仍需手写 include 且无法承载 head 层，去重不彻底；(b) JS 动态渲染导航——违背项目服务端渲染习惯、引入闪烁与无 JS 退化问题；(c) 路由统一传 nav 参数——侵入全部 10 个页面路由处理函数。Jinja2 继承零新增依赖，一次性消除 8 处复制。

### D2. 当前位置声明：子模板顶部 `{% set %}` 再 `{% extends %}`

每个子模板第一行声明 `{% set nav_section = "market|data|setting|none" %}` 与 `{% set nav_item = "..." %}`，`base.html` 以 `NAV` 定义表（分区 → [(条目, href)] 列表）渲染主导航与子导航，`active`/`aria-current` 由条目标识匹配产生。Jinja2 支持 extends 前的顶层 `{% set %}`（渲染时进入上下文），base.html 顶部以注释固化该约定。

备选否弃：(a) `request.url.path` 前缀推导——映射逻辑埋进模板，`/` 与管理路径特判多、难维护；(b) 路由传参——同 D1(c)。

### D3. 分区与条目映射（固定表）

- 行情首页 `market`：行情 `/`、自选管理 `/watchlist`、标签管理 `/tags`
- 数据管理 `data`：股票数据 `/admin/data`、个股历史 `/admin/data/stocks`、ETF数据 `/admin/data/etf`、ETF历史 `/admin/data/etf/history`
- 系统设置 `setting`：用户管理 `/admin/users`、系统状态 `/admin/status`

「数据管理」「系统设置」分区（主导航项 + 对应子导航）以 `{% if current_user.is_admin %}` 整体不渲染给普通用户；普通用户主导航仅「行情首页」。管理页本体仍由 `require_admin_page` 守卫，导航仅是入口层。

### D4. 用户菜单：纯 CSS hover 下拉

顶栏右侧 `.user` 容器：按钮（`<button type="button">`）`👤 {{ current_user.username }} ▾`（管理员附加「管理员」标注，普通用户无后缀——现状普通用户显示「（用户）」，本设计简化为无后缀）；`:hover` 或 `:focus-within` 展开菜单（纯 CSS 无 JS；按钮为可聚焦元素，键盘 Tab 可展开，避免旧导航直链被移除后的键盘可达性回退；菜单 `top:100%` 消除 hover 空隙死区）。菜单项：「修改密码」（`/change-password`）与「退出登录」（保留 `id="logout-link"`，沿用 `bindLogout()` 的 POST `/api/auth/logout` → `/login`，登出行为零改动）。

有意偏差：设计稿按钮仅显示 `admin ▾`，本设计保留管理员标注以免丢失角色信息。

### D5. ETF 占位页：`/admin/data/etf` 与 `/admin/data/etf/history`

`app/main.py` 新增两个 GET 路由（`require_admin_page`，上下文 `current_user`），模板 `admin_data_etf.html`、`admin_data_etf_history.html`：extends base、`data-page="admin-data-etf"`/`"admin-data-etf-history"`（为将来 JS 预留，本期无 init 函数）、内容区单个 `.panel`「敬请期待」空状态。不提供任何数据操作。

备选否弃：`/admin/data/etfs`、`/admin/etf/*` 等命名——选现方案与既有 `/admin/data/stocks`（个股历史）形成对称，为未来 ETF 同步页面保留清晰层级。

### D6. 视觉规范：导航 chrome 用设计稿色板，内容区不动

顶栏深色 `#0f172a`（64px，logo 20px 粗体；`nav-item` 默认 `#cbd5e1`，hover/active 背景 `#1e293b` 白字）；子栏白底 52px、底边框 `#e5e7eb`，`sub-item` 圆角 pill，active 背景 `#dbeafe`、文字 `#2563eb`。沿用现有类命名惯例（扁平 kebab-case），新增 `.logo`、`.nav-main`、`.nav-item`、`.subbar`、`.sub-item`、`.user`、`.user-btn`、`.user-menu`，重定义 `.topbar`（白底 → 深色）。

有意偏差：设计稿 `.content`/`.card` 为示意，内容区沿用现有 `.container` + `.panel`；`index.html` 页脚版本号（`.footer`）保留。

### D7. 无分区页面（`/change-password`）的导航

渲染顶栏（logo + 主导航无 active + 用户菜单），不渲染子导航栏（空子栏视觉上突兀）。备选否弃：归入「系统设置」——修改密码是用户级自助操作，非管理员功能，语义不成立；普通用户也看不到该分区。

### D8. 一致化修复随迁移一并完成

`<title>` 统一「页面名 · MarketMind」、logo「📈 MarketMind」（branding 统一，替代「股票与 ETF 行情看板/行情看板」混用）；CSRF meta 统一由 `base.html` 注入（修复 `admin_status.html` 缺失）；顶栏不再放 h1，各页内容区顶部补 `<h1 class="page-heading">`（页面名，样式低调）保持标题语义；`login.html`、`setup.html` 不 extends base、零改动。

### D9. JS 与构建约束

`app/static/app.js` 不新增任何导航逻辑（active 由服务端输出 class，下拉纯 CSS）；`data-page` 分发、轮询（index 60s、admin-data 运行中 10s、admin-data-stocks 10s/30s）原样保留；不引入前端框架/构建链。

## Risks / Trade-offs

- [8 个模板同时重写，回归面大] → 仅动 head/header 层与外围容器，内容区关键元素（表格/表单/弹层 id 与 class）不动；集成测试逐页断言导航结构、active、角色可见性；手动冒烟清单（见 tasks 末组）。
- [深色顶栏与浅色内容风格冲突] → 仅导航 chrome 用深色，内容区样式不动（设计稿即此意图）。
- [纯 CSS hover 下拉在触屏设备不可用] → 项目现状无任何响应式，桌面优先为既定非目标。
- [base.html 与 `admin_status` 特有上下文（jobs/providers）冲突] → 上下文仍由各自路由传入，base 只消费 `current_user`/`app_version`，jobs/providers 在子模板内容块内使用。
- [`{% set %}` before `{% extends %}` 属较少见的 Jinja 用法] → base.html 顶部注释固化约定，新页面照抄既有子模板模式。
- [移除 dashboard-ui『导航栏用户信息』造成 spec 断层] → `site-navigation`『用户菜单』完整承接其规范内容（用户名展示、修改密码/退出登录入口、登出撤销 Session 跳 `/login`）。

## Migration Plan

1. 落地样式与 `base.html`（不接线），随后逐页改造：业务分区三页 → 管理分区四页 → `change_password.html`。
2. 新增 ETF 占位页路由与模板。
3. 更新既有导航断言、新增导航/占位页测试，全量离线回归 `.venv/bin/python -m pytest -m "not online" -q`。
4. 文档与版本：`docs/CHANGELOG.md` v0.5.0 条目、`docs/README.md` 页面清单、`app/version.py` 升 v0.5.0。
5. 部署：重启容器即生效（纯模板/静态资源变更，无数据迁移与配置变更）；回滚：git revert 单提交。

## Open Questions

1. （已决）ETF 占位页路由命名：`/admin/data/etf`、`/admin/data/etf/history`——与 `/admin/data/stocks` 对称，保留未来层级空间。
2. （已决）普通用户主导航仅剩「行情首页」一项是否单薄：保留——分区反映站点结构，后续能力扩展自然填充，且子导航仍提供三个条目。
