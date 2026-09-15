## MODIFIED Requirements

### Requirement: 登录认证 API

系统 SHALL 提供 `POST /api/auth/login`(用户名+密码,登录成功创建新 Session 并下发 Cookie)、`POST /api/auth/logout`(撤销当前 Session)、`GET /api/auth/me`(返回当前用户 username/role)、`POST /api/auth/change-password`(校验旧密码后更新密码哈希并撤销该用户全部既有 Session) 以及匿名首访初始化接口 `POST /api/auth/setup`。用户名匹配 SHALL 大小写不敏感。登录接口和仅在未初始化状态可用的 setup 接口 SHALL NOT 要求 CSRF Token；setup 成功 SHALL 创建或认领唯一启用的 admin 首用户并下发标准 Session Cookie。已有真实用户后 setup SHALL 返回 409。

#### Scenario: 正确密码登录成功
- **WHEN** POST `/api/auth/login` 传入正确用户名与密码
- **THEN** 返回 200,Set-Cookie 下发 `marketmind_session`,响应含用户基本信息

#### Scenario: 错误密码登录失败
- **WHEN** POST `/api/auth/login` 传入错误密码
- **THEN** 返回 401,不下发 Session Cookie

#### Scenario: 不存在的用户登录失败
- **WHEN** POST `/api/auth/login` 传入不存在的用户名
- **THEN** 返回 401,错误信息不区分"用户不存在"与"密码错误"

#### Scenario: 禁用用户不能登录
- **WHEN** `is_active=false` 的用户尝试登录
- **THEN** 返回 401(或 403),不创建 Session

#### Scenario: 用户名大小写不敏感
- **WHEN** 用户名为 `Alice` 的用户以 `alice` 登录
- **THEN** 匹配同一账户,密码正确时登录成功

#### Scenario: 修改自己的密码
- **WHEN** 登录用户 POST `/api/auth/change-password` 传正确旧密码与新密码
- **THEN** 返回 200,密码更新,该用户其他 Session 全部失效(当前 Session 亦失效,需重新登录)

#### Scenario: 旧密码错误
- **WHEN** POST `/api/auth/change-password` 传错误旧密码
- **THEN** 返回 401,密码不变更

#### Scenario: 匿名创建首用户
- **WHEN** 用户表处于未初始化状态时匿名 POST `/api/auth/setup` 传入合法用户名、密码和确认密码
- **THEN** 返回 200,创建或认领启用的 admin 首用户并下发 `marketmind_session`

#### Scenario: 初始化完成后 setup 被拒绝
- **WHEN** 用户表存在真实用户时匿名 POST `/api/auth/setup`
- **THEN** 返回 409,不创建用户且不下发 Session

### Requirement: 认证与授权 Dependency

系统 SHALL 提供认证依赖 `require_user`(验证 Session → 加载用户 → 检查 is_active → 返回 `CurrentUser(user_id, role)`)与授权依赖 `require_admin`(在 require_user 基础上要求 `role == "admin"`)。未登录访问私有 API SHALL 返回 401;未登录访问私有页面在系统已初始化时 SHALL 返回 302 重定向 `/login`;已登录普通用户访问 admin API SHALL 返回 403。系统未初始化时，匿名访问需要登录的页面入口 SHALL 引导至 `/setup`，但 `/health`、静态资源、`/setup` 和认证 setup API 仍可匿名访问。

#### Scenario: 匿名访问私有 API
- **WHEN** 未携带 Session Cookie 请求 `/api/quotes`
- **THEN** 返回 401 Unauthorized

#### Scenario: 已初始化系统匿名访问私有页面
- **WHEN** 已存在真实用户且未登录访问 `/`
- **THEN** 返回 302 重定向到 `/login`

#### Scenario: 未初始化系统访问私有页面
- **WHEN** 用户表处于未初始化状态且匿名访问 `/`
- **THEN** 返回 302 重定向到 `/setup`

#### Scenario: 普通用户访问 admin API
- **WHEN** role=user 的登录用户请求 `/api/admin/status`
- **THEN** 返回 403 Forbidden

#### Scenario: 管理员访问 admin API
- **WHEN** role=admin 的登录用户请求 `/api/admin/status`
- **THEN** 正常返回业务数据

### Requirement: 登录页面

系统 SHALL 提供 `/login` 页面(Jinja2,匿名可访问),含用户名/密码表单与错误提示;登录成功后跳转首页。系统未初始化时，匿名访问 `/login` SHALL 重定向 `/setup`；系统已初始化时 SHALL 正常展示登录表单。初始化完成后 `/setup` SHALL 不再展示可提交的首用户创建表单。

#### Scenario: 已初始化登录页渲染
- **WHEN** 用户表存在真实用户时匿名访问 `/login`
- **THEN** 返回 200 登录表单页面

#### Scenario: 未初始化登录页引导
- **WHEN** 用户表处于未初始化状态时匿名访问 `/login`
- **THEN** 返回 302 重定向 `/setup`

#### Scenario: 登录失败提示
- **WHEN** 提交错误密码
- **THEN** 页面展示"用户名或密码错误"类提示,不泄露具体失败原因

#### Scenario: 初始化后 setup 不可重复提交
- **WHEN** 首用户已经创建后访问 `/setup`
- **THEN** 页面重定向 `/login` 或展示初始化已完成提示，不显示可用的匿名创建表单
