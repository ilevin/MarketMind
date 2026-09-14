# user-data-isolation Specification

## Purpose
TBD - created by archiving change multi-user-auth. Update Purpose after archive.
## Requirements
### Requirement: 全局共享与用户私有的数据边界

系统 SHALL 维护明确的数据边界:证券主数据(`instrument`)、行情快照(`quote_snapshot`)、基本面快照(`fundamental_snapshot`)、交易日历(`trading_calendar`)、任务状态(`job_status`)、系统设置(`app_setting`)为全局共享,SHALL NOT 按用户重复保存;`watchlist`、`index_watchlist`、`tag`、`watchlist_tag` 为用户私有,每条数据 MUST 归属唯一 `user_id`。多用户关注同一证券时 `instrument` 与行情快照仍只有一份。

#### Scenario: 共享证券主数据
- **WHEN** 用户 A 与用户 B 先后添加同一证券 CN:STOCK:600519
- **THEN** instrument 表仍只有一条 600519 记录,行情快照只有一份,A、B 各自的 watchlist 各有一条记录

#### Scenario: 删除自选不影响全局数据
- **WHEN** 用户 A 删除自选 600519 且用户 B 仍关注该证券
- **THEN** A 的 watchlist 记录删除,B 的记录、instrument 与 quote_snapshot 数据均保留

### Requirement: Repository 层强制用户作用域

用户私有数据的读写 SHALL 由用户作用域 Repository 承担,构造时 `user_id` 为必填参数(无默认值),所有查询 SHALL 在 SQL 层包含 `user_id` 过滤条件,SHALL NOT 在 API 返回前做内存过滤。系统 SHALL NOT 提供 `user_id=None` 表示"查询全部"的语义;跨用户的系统级查询(如后台刷新集合)SHALL 使用独立的 System 作用域 Repository,仅限后台任务使用。

#### Scenario: 查询自动限定当前用户
- **WHEN** 用户 A 通过 `WatchlistRepository(session, user_a_id).list()` 查询
- **THEN** 生成 SQL 含 `WHERE user_id = :user_a_id`,仅返回 A 的自选

#### Scenario: user_id 必填
- **WHEN** 构造用户作用域 Repository 未提供 user_id
- **THEN** 构造失败(类型/参数错误),不存在静默查全库路径

### Requirement: 身份由服务端注入

用户数据归属 SHALL 由服务端从认证 Dependency 解析的 `CurrentUser` 注入 Service/Repository;业务 API SHALL NOT 通过查询参数(如 `?user_id=`)或请求体接受数据归属人指定。

#### Scenario: 携带 user_id 参数无效
- **WHEN** 登录用户 A 请求 `/api/watchlist?user_id=<B 的 id>`
- **THEN** user_id 参数被忽略,仍返回 A 的自选

### Requirement: 越权访问返回 404

用户访问不属于自己的私有资源(如其他用户的 tag_id)SHALL 返回 404,SHALL NOT 返回 403 或任何泄露资源存在性/属主的信息。

#### Scenario: 跨用户 tag_id 返回 404
- **WHEN** 用户 A 使用用户 B 的 tag_id 请求 PATCH `/api/tags/{tag_id}`
- **THEN** 返回 404(与不存在的 tag_id 行为一致)

#### Scenario: 跨用户标签绑定被拒绝
- **WHEN** 用户 A PUT 自选 tags 时传入用户 B 的 tag_id
- **THEN** 返回 404,不创建关联

### Requirement: 管理员不绕过隔离

admin 角色表达"可管理账户与系统",SHALL NOT 因此自动获得读取或修改其他用户私有自选/标签数据的能力;管理员的个人私有数据 SHALL 与普通用户走相同的 `user_id` 过滤。

#### Scenario: 管理员私有数据同样隔离
- **WHEN** admin 用户与普通用户 A 各有自己的自选
- **THEN** admin 调用 `/api/watchlist` 仅返回 admin 自己的自选,与普通用户行为一致

#### Scenario: 管理员无法读取他人私有数据
- **WHEN** admin 尝试访问用户 A 的标签(如以 A 的 tag_id 请求)
- **THEN** 返回 404,不因 role=admin 而放行

