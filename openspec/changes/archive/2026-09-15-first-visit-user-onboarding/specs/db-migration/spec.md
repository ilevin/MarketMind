## MODIFIED Requirements

### Requirement: 多用户数据迁移

`0002_multi_user_auth` 迁移 SHALL 创建唯一的 legacy owner：用户名初始为 `admin`、角色为 `admin`、密码为不可登录的占位哈希、`must_change_password=true`，并将旧单用户数据全部归属该账户。后续首次访问 `/setup` 时，使用者提交的用户名和密码 SHALL 认领该账户；认领 SHALL 保留原 `user_id` 以及 watchlist、index_watchlist、tag、watchlist_tag 中的全部私有数据。迁移文件与配置 SHALL NOT 包含任何明文默认密码。

#### Scenario: 迁移占位账户可被自定义用户名认领
- **WHEN** v0.1.0 数据库升级到 v0.2.0，服务启动后在受控网络访问 `/setup` 并提交合法用户名和密码
- **THEN** 系统更新占位账户的用户名为提交值、写入新的 Argon2id 密码并关闭 `must_change_password`，原 `user_id` 与全部私有数据保持不变

### Requirement: 升级部署流程

数据库升级 SHALL 由现有“容器启动先执行 `alembic upgrade head`，成功后才启动应用”机制自动完成，SHALL NOT 要求用户手动执行迁移。升级后，文档和启动日志 SHALL 优先提示在受控网络访问 `/setup` 认领迁移生成的占位管理员；首用户的用户名 SHALL 由使用者自行设置，不固定为 `admin`。无法使用浏览器时，运维停止应用后 SHALL 可使用现有 CLI 设置占位账户密码或创建管理员作为后备路径。CLI 与应用不得同时打开同一个 DuckDB 文件。

#### Scenario: 旧版本容器升级
- **WHEN** 使用含 0001 数据的旧数据库启动新版本容器
- **THEN** 启动过程自动完成 0002 迁移，应用就绪，旧数据归属占位 legacy owner，并提示通过 `/setup` 完成认领

#### Scenario: 迁移失败阻止启动
- **WHEN** 0002 迁移校验失败
- **THEN** 容器退出，uvicorn 不启动，数据库保持迁移前状态

#### Scenario: CLI 后备初始化
- **WHEN** 运维无法使用浏览器引导
- **THEN** 运维停止应用后可运行现有 CLI 设置占位账户密码或创建管理员，重新启动应用后登录；CLI 执行期间应用不得占用同一数据库文件
