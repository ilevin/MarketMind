# dashboard-ui Specification(Delta)

## MODIFIED Requirements

### Requirement: 行情首页

系统 SHALL 在 `/` 提供首页(要求登录,未登录 302 重定向 `/login`),自上而下为:市场状态区、指数行情卡片区、自选股票/ETF 行情表格(均为当前登录用户的数据)。指数 SHALL NOT 与股票/ETF 混在同一表格。

#### Scenario: 页面结构
- **WHEN** 登录用户访问 /
- **THEN** 依次显示 A股/港股市场状态、指数横向卡片、自选表格

#### Scenario: 未登录重定向
- **WHEN** 未登录访问 /
- **THEN** 302 重定向到 /login

### Requirement: 前端轮询策略

页面首次加载 SHALL 立即读取 /api/quotes 与 /api/indices;市场交易中每 60 秒读取一次;两市场均非 OPEN(午休/收盘/节假日)时 SHALL 停止自动轮询但保留已显示数据。SHALL NOT 整页刷新,SHALL NOT 引入前端框架。所有 fetch 请求 SHALL 统一附带 `X-CSRF-Token` 请求头(由页面 meta 注入)。

#### Scenario: 首次加载
- **WHEN** 收盘后打开首页
- **THEN** 立即读取一次缓存并显示收盘数据,不启动轮询

#### Scenario: 交易中轮询
- **WHEN** 任一市场 OPEN
- **THEN** 每 60 秒读取一次缓存 API 并局部更新

#### Scenario: 空自选
- **WHEN** 当前用户无任何自选标的
- **THEN** 显示「还没有自选标的,去添加一个」提示

#### Scenario: 写请求携带 CSRF 头
- **WHEN** 页面执行任何写操作(添加/删除/排序/标签关联)
- **THEN** fetch 请求携带页面 meta 注入的 X-CSRF-Token

## ADDED Requirements

### Requirement: 登录页面

系统 SHALL 提供匿名可访问的 `/login` 页面(详见 user-authentication 规格):用户名/密码表单、错误提示、登录成功跳转首页;`/login`、`/health`、`/static/*` SHALL 为仅有的匿名可访问路由。

#### Scenario: 登录成功跳转
- **WHEN** 在 /login 提交正确凭据
- **THEN** 跳转到 / 并以登录态展示首页

### Requirement: 导航栏用户信息

业务页面导航栏 SHALL 显示当前用户名与角色,提供「修改密码」与「退出登录」入口;管理员额外显示「用户管理」与「系统状态」入口。退出登录 SHALL 调用 logout API 并跳转 /login。

#### Scenario: 普通用户导航栏
- **WHEN** role=user 登录后查看任意业务页面
- **THEN** 导航栏显示用户名、角色,含修改密码与退出登录,不含用户管理入口

#### Scenario: 管理员导航栏
- **WHEN** role=admin 登录后查看任意业务页面
- **THEN** 导航栏额外显示用户管理入口

#### Scenario: 退出登录
- **WHEN** 点击导航栏退出登录
- **THEN** Session 撤销,跳转 /login,回退按钮无法恢复登录态

### Requirement: 修改密码页面

系统 SHALL 提供修改密码界面(旧密码 + 新密码 + 确认新密码),成功后要求重新登录。

#### Scenario: 修改密码成功
- **WHEN** 提交正确旧密码与合规新密码
- **THEN** 提示成功,当前 Session 失效,跳转 /login 重新登录

### Requirement: 管理员用户管理页面

系统 SHALL 提供管理员专属 `/admin/users` 页面(详见 user-management 规格),与既有 Jinja2 页面风格一致,不引入前端框架。

#### Scenario: 管理员使用用户管理页面
- **WHEN** admin 在 /admin/users 创建用户、切换启用状态、重置密码
- **THEN** 各操作即时反馈结果,列表刷新展示最新状态
