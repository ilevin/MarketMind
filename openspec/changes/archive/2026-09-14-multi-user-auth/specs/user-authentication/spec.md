# user-authentication Specification(Delta)

## ADDED Requirements

### Requirement: 登录认证 API

系统 SHALL 提供 `POST /api/auth/login`(用户名+密码,登录成功创建新 Session 并下发 Cookie)、`POST /api/auth/logout`(撤销当前 Session)、`GET /api/auth/me`(返回当前用户 username/role)、`POST /api/auth/change-password`(校验旧密码后更新密码哈希并撤销该用户全部既有 Session)。用户名匹配 SHALL 大小写不敏感。登录接口 SHALL NOT 要求 CSRF Token(登录前无 Session)。

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

### Requirement: Session Cookie 安全

浏览器侧 SHALL 仅持有服务端生成的高强度随机 Session Token(HttpOnly Cookie `marketmind_session`,SameSite=Lax,Path=/);数据库 SHALL 只存 `SHA-256(token)`,SHALL NOT 存明文 Token。Cookie SHALL NOT 携带 user_id、role、用户名或权限信息。生产 HTTPS 环境 SHALL 启用 `Secure` 属性,本地 HTTP 开发环境可通过配置关闭。日志 SHALL NOT 输出密码、Session Token、CSRF Token。

#### Scenario: Cookie 属性
- **WHEN** 登录成功检查 Set-Cookie
- **THEN** Cookie 为 HttpOnly、SameSite=Lax、Path=/;生产配置下含 Secure

#### Scenario: 数据库不存明文 Token
- **WHEN** 检查 user_session 表内容
- **THEN** session_token_hash 为 SHA-256 摘要,不存在可逆或明文 Token

### Requirement: 密码安全存储

密码 SHALL 使用 Argon2id 哈希存储,SHALL NOT 保存明文或可逆加密形式。

#### Scenario: 密码哈希存储
- **WHEN** 创建用户或设置密码后检查 app_user.password_hash
- **THEN** 存储值为 Argon2id 哈希,无法还原明文

### Requirement: Session 生命周期

Session SHALL 有明确 `expires_at`,默认有效期 7 天、可通过配置项(如 `session.ttl_days`)调整。登录 SHALL 创建新 Session(登录时旋转);退出登录 SHALL 立即撤销当前 Session;禁用用户、重置密码 SHALL 撤销该用户全部 Session;修改角色 SHALL 撤销其既有 Session。系统 SHALL NOT 在每个请求更新 last_seen_at(避免高频写)。

#### Scenario: Session 过期
- **WHEN** 使用已超过 expires_at 的 Session Cookie 访问私有 API
- **THEN** 返回 401

#### Scenario: Session 被撤销
- **WHEN** 用户 A 退出登录后,B(或 A 自己)持旧 Cookie 访问私有 API
- **THEN** 返回 401

#### Scenario: 密码重置后旧 Session 失效
- **WHEN** 管理员重置用户 A 的密码后,A 持旧 Session Cookie 访问
- **THEN** 返回 401,需用新密码重新登录

#### Scenario: 用户禁用后 Session 失效
- **WHEN** 管理员禁用用户 A 后,A 持既有 Session Cookie 访问
- **THEN** 返回 401(或 302 → /login)

### Requirement: 认证与授权 Dependency

系统 SHALL 提供认证依赖 `require_user`(验证 Session → 加载用户 → 检查 is_active → 返回 `CurrentUser(user_id, role)`)与授权依赖 `require_admin`(在 require_user 基础上要求 `role == "admin"`)。未登录访问私有 API SHALL 返回 401;未登录访问私有页面 SHALL 返回 302 重定向 `/login`。已登录普通用户访问 admin API SHALL 返回 403。

#### Scenario: 匿名访问私有 API
- **WHEN** 未携带 Session Cookie 请求 `/api/quotes`
- **THEN** 返回 401 Unauthorized

#### Scenario: 匿名访问私有页面
- **WHEN** 未登录访问 `/`
- **THEN** 返回 302 重定向到 `/login`

#### Scenario: 普通用户访问 admin API
- **WHEN** role=user 的登录用户请求 `/api/admin/status`
- **THEN** 返回 403 Forbidden

#### Scenario: 管理员访问 admin API
- **WHEN** role=admin 的登录用户请求 `/api/admin/status`
- **THEN** 正常返回业务数据

### Requirement: CSRF 防护

由于认证使用 Cookie,所有改变状态的请求(POST/PUT/PATCH/DELETE,登录接口除外)SHALL 校验 `X-CSRF-Token` 请求头与 Session 关联的 CSRF Token 一致;不一致 SHALL 返回 403。Jinja2 页面 SHALL 通过 `<meta name="csrf-token">` 注入 Token,前端 `fetch()` SHALL 统一附加该头。

#### Scenario: 缺失 CSRF Token 的写请求
- **WHEN** 已登录用户 POST `/api/watchlist` 不带 X-CSRF-Token
- **THEN** 返回 403,不执行业务逻辑

#### Scenario: CSRF Token 错误
- **WHEN** 已登录用户 POST 携带错误的 X-CSRF-Token
- **THEN** 返回 403

#### Scenario: 携带正确 CSRF Token
- **WHEN** 已登录用户 POST 携带页面注入的正确 X-CSRF-Token
- **THEN** 请求正常处理

#### Scenario: GET 请求不要求 CSRF
- **WHEN** 已登录用户 GET `/api/watchlist` 不带 X-CSRF-Token
- **THEN** 正常返回数据

### Requirement: 登录限速

系统 SHALL 实现进程内登录限速(按 IP + username 维度):连续失败达到阈值(如 5 次)后,在时间窗口(如 5 分钟)内拒绝该维度的登录尝试;SHALL NOT 为此引入 Redis 等外部依赖。

#### Scenario: 连续失败后限速
- **WHEN** 同一 IP 对同一用户名连续登录失败达到阈值后再次尝试
- **THEN** 返回 429(或明确限速提示),即使此次密码正确也不创建 Session

#### Scenario: 窗口外恢复
- **WHEN** 限速窗口过后再次正确登录
- **THEN** 登录成功,失败计数重置

### Requirement: 登录页面

系统 SHALL 提供 `/login` 页面(Jinja2,匿名可访问),含用户名/密码表单与错误提示;登录成功后跳转首页。

#### Scenario: 登录页渲染
- **WHEN** 匿名访问 `/login`
- **THEN** 返回 200 登录表单页面

#### Scenario: 登录失败提示
- **WHEN** 提交错误密码
- **THEN** 页面展示"用户名或密码错误"类提示,不泄露具体失败原因
