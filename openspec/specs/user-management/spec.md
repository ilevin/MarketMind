# user-management Specification

## Purpose
TBD - created by archiving change multi-user-auth. Update Purpose after archive.
## Requirements
### Requirement: 管理员用户管理 API

系统 SHALL 提供管理员专属用户管理 API:`GET /api/admin/users`(列表)、`POST /api/admin/users`(创建,密码必填或由管理员设定)、`PATCH /api/admin/users/{user_id}`(修改角色/启用/禁用/基本信息)、`POST /api/admin/users/{user_id}/reset-password`(重置密码并撤销该用户全部 Session)。第一阶段 SHALL NOT 提供 `DELETE /api/admin/users/{user_id}` 物理删除。所有用户管理 API SHALL 要求 admin 角色。

#### Scenario: 管理员查看用户列表
- **WHEN** admin 请求 GET `/api/admin/users`
- **THEN** 返回 200,含 username、role、is_active、created_at、last_login_at 等字段;SHALL NOT 返回 password_hash

#### Scenario: 管理员创建用户
- **WHEN** admin POST `/api/admin/users` `{"username":"bob","password":"...","role":"user"}`
- **THEN** 返回 201,新用户可登录

#### Scenario: 普通用户访问用户管理 API
- **WHEN** role=user 请求 GET `/api/admin/users`
- **THEN** 返回 403

#### Scenario: 重置密码撤销 Session
- **WHEN** admin 对用户 A 执行 reset-password
- **THEN** A 的全部既有 Session 被撤销,A 需用新密码重新登录

#### Scenario: 不提供物理删除
- **WHEN** 检查用户管理 API 路由
- **THEN** 不存在 DELETE `/api/admin/users/{user_id}` 端点

### Requirement: 用户名规则与唯一性

用户名 SHALL 为 3–32 字符、仅含 `[A-Za-z0-9_-]`(不支持中文);唯一性 SHALL 大小写不敏感(按小写比较,`Alice` 与 `alice` 视为冲突)。用户名查重 SHALL 沿用现有写锁内查询查重模式(与 `tag.name` 一致),不依赖数据库 UNIQUE 约束。

#### Scenario: 重复用户名禁止
- **WHEN** 已存在 `alice` 时创建 `Alice`
- **THEN** 返回 409,不创建

#### Scenario: 非法字符禁止
- **WHEN** 创建用户名含中文或空格等非法字符
- **THEN** 返回 422,不创建

#### Scenario: 长度限制
- **WHEN** 用户名少于 3 或多于 32 字符
- **THEN** 返回 422

### Requirement: 禁用优先于删除

用户管理第一阶段 SHALL 仅支持 `is_active=false` 禁用,SHALL NOT 物理删除用户及其自选/标签数据;禁用用户的全部 Session SHALL 立即失效;重新启用后可再次登录。

#### Scenario: 禁用用户
- **WHEN** admin PATCH `/api/admin/users/{id}` `{"is_active": false}`
- **THEN** 该用户既有 Session 全部失效,无法登录

#### Scenario: 重新启用
- **WHEN** admin 对已禁用用户 PATCH `{"is_active": true}`
- **THEN** 该用户可凭原密码重新登录,其自选/标签数据完整保留

### Requirement: 最后一个管理员保护

系统 SHALL 防止失去全部可登录管理员:禁用最后一个有效管理员或将其降级为 user 的操作 SHALL 被拒绝(409)。

#### Scenario: 禁用最后一个管理员被拒绝
- **WHEN** 系统仅剩一个 is_active 的 admin,尝试禁用该账户
- **THEN** 返回 409,该管理员保持启用

#### Scenario: 降级最后一个管理员被拒绝
- **WHEN** 系统仅剩一个有效 admin,尝试将其角色改为 user
- **THEN** 返回 409,角色保持 admin

#### Scenario: 存在其他管理员时允许操作
- **WHEN** 系统有两个有效 admin,禁用其中一个
- **THEN** 操作成功

### Requirement: 管理 CLI

系统 SHALL 提供管理 CLI:`python -m app.cli users set-password <username>`(设置/重置密码,撤销该用户 Session)、`python -m app.cli users create <username>`(创建用户)、`python -m app.cli users promote <username>`(提升为 admin)。密码 SHALL 通过终端安全输入,SHALL NOT 出现在 shell history、参数或仓库文件中。

#### Scenario: CLI 设置密码
- **WHEN** 运行 `python -m app.cli users set-password admin` 并按提示输入密码
- **THEN** 密码以 Argon2id 哈希更新,该用户既有 Session 撤销

#### Scenario: CLI 创建第一个管理员
- **WHEN** 全新部署运行 `users create` 后 `users promote`
- **THEN** 创建的 admin 可登录并使用用户管理功能

### Requirement: 公开注册默认关闭

系统 SHALL 默认关闭公开注册,账户仅由管理员(或 CLI)创建;SHALL NOT 提供匿名可调用的注册端点。未来如开放注册,公开注册 SHALL 只能创建 `user` 角色。

#### Scenario: 无公开注册端点
- **WHEN** 检查 API 路由
- **THEN** 不存在匿名可用的 POST 注册接口

### Requirement: 用户管理页面

系统 SHALL 提供管理员专属页面 `/admin/users`(Jinja2):用户列表(用户名/角色/状态/创建时间)、创建用户、启用/禁用、角色分配、重置密码操作。

#### Scenario: 管理员访问用户管理页
- **WHEN** admin 访问 `/admin/users`
- **THEN** 返回 200 用户管理页面

#### Scenario: 普通用户访问被拒
- **WHEN** role=user 访问 `/admin/users`
- **THEN** 返回 403(或重定向无权限提示页)

