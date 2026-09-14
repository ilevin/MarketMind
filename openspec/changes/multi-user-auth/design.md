# 设计:多用户支持与用户认证

> 完整技术背景见仓库根目录 `MarketMind_multi_user_auth_technical_plan.md`,本设计为其工程化落地决策。

## Context

MarketMind 是 FastAPI + Jinja2 + DuckDB 的单体证券研究工具,`uvicorn --workers 1` 单进程部署,写事务由 `WriteCoordinator` 串行化。当前数据模型按单用户假设:`watchlist`/`index_watchlist` 以 `instrument_id` 为全库主键,`tag` 全局唯一,`/api/admin/*` 无真实认证。本次引入多用户后,身份成为业务数据一等维度,但市场数据(instrument/quote_snapshot/fundamental_snapshot/trading_calendar/job_status/app_setting)继续全局共享。

## Goals / Non-Goals

**Goals:**
- 多用户独立登录,`user`/`admin` 两角色,默认拒绝一切未授权访问
- watchlist/index_watchlist/tag/watchlist_tag 按用户隔离,隔离发生在数据库查询层
- 旧单用户数据自动迁移给 legacy owner,数据库升级由现有启动时自动迁移机制完成
- 保持 FastAPI + Jinja2 + DuckDB 单进程/单 Writer 架构不变

**Non-Goals:**
- OAuth/SSO/第三方登录、RBAC 多角色、组织/租户模型
- 公开注册(默认关闭,管理员创建账户)
- 用户物理删除(仅禁用)
- PostgreSQL/Redis 迁移、多实例部署
- 管理员查看/代管其他用户私有数据

## Decisions

### D1 认证:服务端 Session + HttpOnly Cookie(非 JWT)
同源 Jinja2 单体,无移动端/第三方 Token 分发需求;Session 的失效、禁用、重置处理更直接,无需 Refresh Token。Cookie `marketmind_session`:HttpOnly、SameSite=Lax、Path=/;`Secure` 由配置控制(生产 HTTPS 开启,本地 HTTP 开发关闭)。Cookie 只存高强度随机 token,数据库只存 `SHA-256(token)`。

### D2 密码:Argon2id(通过 `pwdlib`)
只存哈希,不可逆。密码重置后撤销该用户全部 Session。

### D3 角色:单一 `role` 列(`user`/`admin`)
不引入 role/permission/user_role 多表 RBAC;出现更复杂角色需求时再演进。

### D4 数据边界:私有表加 `user_id`,全局表不动
- `watchlist`/`index_watchlist`:PK 改为 `(user_id, instrument_id)`
- `tag`:加 `user_id`,名称唯一范围缩为同一 user 内
- `watchlist_tag`:PK 改为 `(user_id, instrument_id, tag_id)`,FK 指向 `(user_id, instrument_id)`
- 用户名唯一性沿用 `tag.name` 的"写锁内查询查重"模式(与现有 DuckDB 约束兼容策略一致),不做依赖数据库 UNIQUE 约束的前提假设

### D5 隔离架构:用户作用域 Repository,禁用 `user_id=None` 语义
```python
WatchlistRepository(session, user_id)   # 所有查询自动含 WHERE user_id = :uid
SystemWatchlistRepository(session)      # 仅后台任务使用(跨用户 DISTINCT)
```
不允许 `WatchlistRepository(session, user_id=None)` 表示"看全部"。身份由 FastAPI Dependency 解析注入(`CurrentUser(user_id, role)`),普通业务 API 不接受客户端传入 `user_id`。跨用户访问私有资源返回 404(不泄露存在性)。

### D6 授权 Dependency:Router 层统一保护
`app/auth/dependencies.py` 提供 `require_user`(API 401 / 页面 302 → /login)与 `require_admin`(403)。admin 路由在 `APIRouter(dependencies=[Depends(require_admin)])` 层声明,避免新增接口漏加检查。

### D7 CSRF:Session 派生 Token + 双提交
`user_session` 表存 `csrf_token`;Jinja2 页面经 `<meta name="csrf-token">` 注入,前端 `fetch()` 统一带 `X-CSRF-Token` 头;所有 POST/PUT/PATCH/DELETE 强制校验(登录接口除外,登录前无 Session)。

### D8 后台刷新:跨用户 DISTINCT 聚合
`_all_watchlist_ids()` 改为系统级查询 `SELECT DISTINCT instrument_id FROM (watchlist UNION ALL index_watchlist)`,多用户关注同一证券只刷新一次;用户删除自选不影响全局缓存与 instrument 数据。

### D9 迁移:v2 表复制切换(对齐 db-migration 既有策略)
Alembic 新迁移 `0002_multi_user_auth`:创建 `seq_user_id`/`app_user`/`user_session` → 建 4 张 v2 表 → 创建 legacy owner(`admin`,占位密码哈希,`must_change_password=true`)→ 旧数据按 legacy owner 归属拷贝 → 行数/外键校验 → 删旧表 → 改名。复杂主键变更不做原地 ALTER。**数据库升级必须自动完成**:沿用"启动时 `alembic upgrade head` 成功后才启动应用"的现有机制。

### D10 初始管理员:管理 CLI
`python -m app.cli users set-password admin` / `users create <username>` / `users promote <username>`,密码终端安全输入,不进 shell history;新部署也用 CLI 创建第一个管理员。迁移文件与配置中不存任何明文密码。

### D11 用户名规则(已确认)
3–32 字符,仅 `[A-Za-z0-9_-]`;唯一性大小写不敏感(按小写比较),登录同样大小写不敏感;不支持中文。

### D12 Session 生命周期(已确认)
默认 7 天,`session.ttl_days` 配置可调;登录创建新 Session(登录时旋转),退出立即撤销,禁用/重置密码/改角色撤销该用户全部 Session;不逐请求更新 `last_seen_at`(避免高频写)。

### D13 管理员边界
admin 拥有用户/系统管理权,但个人自选与标签走与普通用户完全相同的 `user_id` 过滤,不自动绕过隔离。提供"最后一个有效管理员保护":不能禁用最后一个 admin、不能把最后一个 admin 降级。

### D14 登录限速:进程内轻量实现
按 IP + username 维护失败计数,连续失败后短时间窗口拒绝;不引入 Redis。

## Risks / Trade-offs

- [DuckDB 主键/外键重建迁移风险(数据损坏或半成品残留)] → 严格走"建新表→搬数据→校验→切换"流程,行数与业务键校验失败整体回滚;上线前对生产数据备份,先在测试副本演练
- [Repository 漏传/漏过滤 `user_id` 导致越权] → 构造函数必填 `user_id`(无默认值),静态审查 + 数据隔离集成测试(A/B 双用户交叉访问)强制覆盖
- [前端原生 JS 改造面大(登录页、CSRF 头、导航栏)] → 统一封装 `fetch` 帮助函数注入 CSRF 头;分阶段提交,每阶段可测试可回滚
- [Cookie 认证 + CSRF 引入回归] → 既有 API 集成测试统一补登录态 fixture,先让存量测试在新认证下通过再叠加新断言
- [单进程登录限速在重启后失效] → 可接受(第一阶段目标是防暴力尝试而非严格限流);进程内实现,后续如需持久化再演进
- [多用户下 Session 查询增加每请求一次读] → DuckDB 读性能足够(几十用户量级),且不逐请求写
- [legacy owner 占位密码若从未被 CLI 设置] → 迁移后无人能以 admin 登录属安全失败方向;文档与部署说明明确提示运行 CLI

## Migration Plan

1. **实施顺序**(每个 PR 可测试、可回滚):用户模型+密码工具+AuthService → Session+登录/退出 → admin 路由权限保护 → watchlist/index_watchlist 用户化 → tag/watchlist_tag 用户化 → 旧数据迁移 → 管理员用户管理 → CSRF+安全增强+集成测试
2. **部署步骤**:备份 DuckDB 文件 → 拉取新版本 → 容器启动自动 `alembic upgrade head`(迁移失败容器退出)→ 运行 `python -m app.cli users set-password admin` 设置 legacy owner 密码 → 管理员登录创建普通用户
3. **回滚**:迁移前备份文件直接还原;代码回滚后旧版本 schema 与旧代码一致(迁移不可逆时依赖备份文件恢复)

## Open Questions

- `pwdlib` vs `argon2-cffi` 直接依赖:倾向 `pwdlib`(FastAPI 生态常用、API 简洁),实施时确认与当前 Python 版本兼容性
- 登录限速具体阈值(失败次数/窗口时长):实施时定,倾向 5 次失败 / 5 分钟窗口
