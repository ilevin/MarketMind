## MODIFIED Requirements

### Requirement: 管理 CLI

系统 SHALL 提供管理 CLI:`python -m app.cli users set-password <username>`(设置/重置密码,撤销该用户 Session)、`python -m app.cli users create <username>`(创建用户)、`python -m app.cli users promote <username>`(提升为 admin)。密码 SHALL 通过终端安全输入,SHALL NOT 出现在 shell history、参数或仓库文件中。全新部署和迁移生成的占位管理员 SHALL 也可通过首次访问 `/setup` 创建或认领；CLI SHALL 作为无法访问 Web 引导时的后备初始化方式。

#### Scenario: CLI 设置密码
- **WHEN** 运行 `python -m app.cli users set-password admin` 并按提示输入密码
- **THEN** 密码以 Argon2id 哈希更新,该用户既有 Session 撤销

#### Scenario: CLI 创建第一个管理员
- **WHEN** 全新部署运行 `users create` 后 `users promote`
- **THEN** 创建的 admin 可登录并使用用户管理功能

#### Scenario: 首访创建管理员后 CLI 可继续管理
- **WHEN** 首访流程已创建唯一 admin 后运行 `users create <username>`
- **THEN** CLI 创建普通用户，且不改变首访 admin 的角色、Session 或用户数据

### Requirement: 公开注册默认关闭

系统 SHALL 默认关闭公开注册,账户仅由管理员、CLI 或仅限未初始化状态的一次性 `/api/auth/setup` 创建;SHALL NOT 提供可重复使用的匿名注册端点。未来如开放注册,公开注册 SHALL 只能创建 `user` 角色。首访 setup 接口 SHALL 仅创建第一个启用的 admin，且在任意真实用户存在后关闭。

#### Scenario: 无公开注册端点
- **WHEN** 系统已初始化且检查 API 路由
- **THEN** 不存在可重复使用的匿名 POST 注册接口，setup 返回 409

#### Scenario: 未初始化允许一次首用户引导
- **WHEN** 用户表为空或仅有迁移占位管理员时匿名调用 POST `/api/auth/setup`
- **THEN** 仅允许创建或认领一个启用的 admin，不提供创建普通用户或第二个用户的能力

#### Scenario: 首用户完成后匿名注册关闭
- **WHEN** 首用户已创建后匿名再次调用 POST `/api/auth/setup`
- **THEN** 返回 409，不新增用户
