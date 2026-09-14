# 提案:多用户支持与用户认证(v0.2.0)

## Why

MarketMind 当前所有业务数据按"单用户"假设设计:`watchlist`/`index_watchlist` 以 `instrument_id` 为全库主键、`tag` 为全局命名空间,`/api/admin/*` 没有真实身份认证与管理员授权边界。要支持多人独立使用(家庭/小团队场景),必须把用户身份加入业务数据模型,实现私有数据可靠隔离,同时继续全局共享市场数据并保持 DuckDB 单进程/单 Writer 部署模式。

技术方案详见仓库根目录 `MarketMind_multi_user_auth_technical_plan.md`,本变更按该方案实施。

## What Changes

- 新增身份域:创建 `app_user`(用户、角色 `user/admin`、启用状态)与 `user_session`(服务端 Session)表,密码使用 Argon2id 哈希
- 新增认证:服务端 Session + HttpOnly Cookie(非 JWT),数据库只存 Session Token 的 SHA-256 哈希;登录/退出/修改密码 API 与 `/login` 页面
- 新增 CSRF 防护:所有状态修改请求要求 `X-CSRF-Token` 头
- 新增授权 Dependency:`require_user`/`require_admin`,未登录 API 返回 401、页面重定向 `/login`;admin 路由在 Router 层统一保护
- 新增管理员用户管理:用户列表、创建用户、启用/禁用、角色分配、重置密码,以及"最后一个管理员保护"规则;默认关闭公开注册,第一阶段只禁用不物理删除用户
- 新增管理 CLI(`python -m app.cli users ...`)用于创建初始管理员、设置密码
- **BREAKING** 用户私有表改造:`watchlist`/`index_watchlist`/`tag`/`watchlist_tag` 增加 `user_id`,主键改为含 `user_id` 的复合主键;同一证券可被多用户同时关注,同名标签可跨用户并存
- Repository/Service 改造:用户作用域 Repository 构造时强制 `user_id`,隔离发生在数据库查询层;后台系统查询使用独立的 System Repository
- 后台行情刷新改造:跨用户聚合 `DISTINCT instrument_id`,同一证券全局只刷新一次
- **BREAKING** 数据迁移:现有单用户数据通过 Alembic 迁移(v2 表复制切换模式)归属 legacy owner(admin),含行数与外键校验;数据库升级必须自动完成
- 页面导航栏显示当前用户名/角色/退出登录,管理员额外显示用户管理入口

## Capabilities

### New Capabilities
- `user-authentication`: 登录认证与会话管理——Session Cookie、密码 Argon2id 哈希、登录/退出/修改密码、CSRF 防护、登录限速、Session 生命周期(过期/撤销/禁用失效/密码重置失效)
- `user-management`: 管理员用户管理——用户列表、创建、启用/禁用、角色分配、重置密码、最后一个管理员保护、管理 CLI
- `user-data-isolation`: 用户数据隔离边界——全局共享数据与用户私有数据的划分规则、Repository 层强制 `user_id` 过滤、越权返回 404

### Modified Capabilities
- `watchlist-management`: 自选/指数配置数据归属从全库唯一改为按用户隔离;`watchlist`/`index_watchlist` 主键变为 `(user_id, instrument_id)`;A 删除自选不影响 B
- `tag-management`: 标签命名空间从全库唯一改为同一 `user_id` 内唯一;跨用户同名标签允许;`watchlist_tag` 关系按用户隔离
- `rest-api`: 所有私有业务 API 要求登录并自动以当前用户为作用域,不接受客户端传入 `user_id`;`/api/admin/*` 要求管理员角色
- `quote-cache-refresh`: 后台刷新集合改为跨用户 DISTINCT 聚合,多用户关注同一证券不重复刷新
- `db-migration`: 新增多用户迁移(v2 表复制切换、legacy owner 归属旧数据、行数/外键校验)
- `dashboard-ui`: 新增登录页;业务页面要求登录;导航栏显示用户信息;新增管理员用户管理页面
- `database-persistence`: 业务主键要求扩展——用户私有表使用含 `user_id` 的复合主键;用户名唯一性沿用写锁内查重模式

## Impact

- **代码**:`app/main.py`、`app/api/*`(auth/admin/admin_users/watchlist/index_watchlist/tags)、`app/models/*`(watchlist/tag/watchlist_tag + 新增 user/user_session)、`app/repositories/*`、`app/services/*`(新增 auth_service/user_service)、`app/templates/*`、`app/static/*`
- **新增目录**:`app/auth/`(dependencies/password/session)、`app/cli`
- **数据库**:Alembic 迁移新增 `seq_user_id`/`app_user`/`user_session`,重建 `watchlist`/`index_watchlist`/`tag`/`watchlist_tag` 为 v2 表并切换;全局表(`instrument`/`quote_snapshot`/`fundamental_snapshot`/`trading_calendar`/`job_status`/`app_setting`)不变
- **依赖**:`pyproject.toml` 新增 Argon2 密码哈希库(`pwdlib`/`argon2-cffi` 之一)
- **部署**:继续 `uvicorn --workers 1` 单进程 + `WriteCoordinator`,不引入 PostgreSQL/Redis;登录 Cookie 需生产环境 HTTPS(`Secure` 可配置)
- **测试**:新增认证、权限、数据隔离(最重要)、后台去重、迁移测试;现有行情/基本面/后台任务测试不得退化
- **破坏性**:旧单用户数据库升级后原数据归属 legacy owner(admin),需通过 CLI 设置初始管理员密码;API 未登录访问行为改变(401/302)
