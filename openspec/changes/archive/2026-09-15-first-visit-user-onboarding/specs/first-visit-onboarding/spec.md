## ADDED Requirements

### Requirement: 首次访问创建第一个管理员

系统 SHALL 在未初始化状态提供匿名可访问的 `/setup` 页面和 `POST /api/auth/setup` 接口。未初始化状态定义为用户表为空，或仅存在一个启用的管理员占位账户（密码哈希为迁移约定的 `PLACEHOLDER_HASH` 且不存在其他用户）。提交 SHALL 包含用户名、密码和密码确认；服务端 SHALL 复用现有用户名规则和密码强度校验。成功后 SHALL 创建或认领该唯一首用户，使其为启用的 `admin`，并建立标准 Session。

#### Scenario: 空用户库首次访问
- **WHEN** 用户表为空时匿名访问 `/` 或 `/login`
- **THEN** 页面将用户引导至 `/setup`，且 `/setup` 返回 200 并展示首用户创建表单

#### Scenario: 创建首用户成功
- **WHEN** 匿名用户在未初始化状态提交合法用户名、匹配且合法的密码和确认密码
- **THEN** 返回 200，首用户为启用的 admin，响应下发标准 `marketmind_session` Cookie，且该用户可访问首页和管理员功能

#### Scenario: 占位管理员被认领
- **WHEN** 数据库仅有迁移生成的启用 admin 占位账户时提交合法首用户信息
- **THEN** 系统更新该账户的用户名和 Argon2id 密码哈希而非新增账户，保留原 user_id 及其已迁移的用户私有数据，并建立 Session

#### Scenario: 首用户输入校验失败
- **WHEN** 匿名用户提交非法用户名、弱密码或两次密码不一致
- **THEN** 返回 422，用户表不新增或修改账户，且响应不泄露密码或密码哈希

#### Scenario: 已初始化后禁止匿名创建
- **WHEN** 用户表存在任意真实用户时匿名 POST `/api/auth/setup`
- **THEN** 返回 409，系统不创建或修改任何用户、不下发 Session

#### Scenario: 并发首用户创建
- **WHEN** 两个匿名请求在未初始化状态几乎同时提交合法首用户信息
- **THEN** 至多一个请求成功创建或认领首用户，其他请求返回 409，系统不存在第二个首用户

### Requirement: 首次初始化入口安全边界

`POST /api/auth/setup` SHALL 不要求已有 Session 或 CSRF Token，但 SHALL 只能在未初始化状态执行；接口 SHALL 不接受调用方指定 role、is_active、user_id 或其他权限字段。初始化请求 SHALL 采用进程内 IP 限速，并 SHALL 遵守日志不记录密码、Session Token、CSRF Token 和密码哈希的约束。

#### Scenario: 初始化接口无需 CSRF
- **WHEN** 匿名用户在未初始化状态 POST `/api/auth/setup` 且不携带 `X-CSRF-Token`
- **THEN** 请求通过 CSRF 层并按初始化业务规则处理

#### Scenario: 初始化参数不能提升权限
- **WHEN** 请求试图提交 role=user、role=admin 以外的权限字段或修改账户状态字段
- **THEN** 系统忽略或拒绝这些额外字段，成功路径仍只创建启用的 admin 首用户

#### Scenario: 初始化成功后入口关闭
- **WHEN** 首用户已创建后再次访问 `/setup` 或调用 `POST /api/auth/setup`
- **THEN** 页面重定向到 `/login` 或接口返回 409，且不执行用户写入
