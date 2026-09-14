# Tasks: multi-user-auth

> 按设计文档 Migration Plan 的 8 个 PR 阶段组织;每组任务完成后应保持测试可运行、可提交。

## 1. 依赖与身份域模型(PR 1)

- [x] 1.1 在 `pyproject.toml` 增加密码哈希依赖(`pwdlib[argon2]`,实施时确认版本兼容)
- [x] 1.2 创建 `seq_user_id` sequence、`app_user`、`user_session` SQLAlchemy 模型(`app/models/user.py`、`app/models/user_session.py`),字段对齐 user-authentication / database-persistence 规格
- [x] 1.3 修改 `watchlist`/`index_watchlist`/`tag`/`watchlist_tag` 模型:增加 `user_id`、复合主键、外键(app_user / watchlist(user_id, instrument_id))
- [x] 1.4 实现 `app/auth/password.py`(Argon2id 哈希与校验)及单元测试

## 2. 用户与 Session Repository/Service(PR 1-2)

- [x] 2.1 实现 `app/repositories/user.py`:按用户名(小写比较)查询、创建(写锁内查重)、更新角色/启用状态/密码哈希、有效管理员计数
- [x] 2.2 实现 `app/repositories/user_session.py`:创建、按 token hash 查询(含过期/撤销判定)、撤销单个、按 user_id 撤销全部
- [x] 2.3 实现 `app/services/auth_service.py`:登录(校验+限速+Session 旋转)、退出、修改密码、Session 校验链(token→hash→session→user→is_active→CurrentUser)
- [x] 2.4 实现 `app/services/user_service.py`:创建用户、启用/禁用、角色分配、重置密码、最后一个管理员保护(409)
- [x] 2.5 单元测试:密码哈希、用户名规则(3-32/字符集/大小写不敏感唯一)、最后一个管理员保护、Session 生命周期(过期/撤销/禁用失效/密码重置失效)

## 3. 认证接入(PR 2)

- [x] 3.1 实现 `app/auth/session.py` 与 `app/auth/dependencies.py`:`get_current_user_optional`/`require_user`(API 401、页面 302 → /login)/`require_admin`(403)
- [x] 3.2 实现 `app/api/auth.py`:POST login/logout、GET me、POST change-password;登录限速(进程内 IP+username,5 次/5 分钟)
- [x] 3.3 实现 `/login` 页面模板与登录跳转;`/login`、`/health`、`/static/*` 之外全部路由要求登录
- [x] 3.4 导航栏增加用户名/角色/修改密码/退出登录(管理员含用户管理入口);实现修改密码界面
- [x] 3.5 集成测试:登录成功/失败/禁用用户/大小写不敏感/Session 过期与撤销/匿名 401 与 302/user→admin 403

## 4. Admin 路由权限保护(PR 3)

- [x] 4.1 `/api/admin/*` Router 层统一声明 `dependencies=[Depends(require_admin)]`
- [x] 4.2 集成测试:匿名→admin API 401、user→admin API 403、admin→admin API 200;`/health` 保持匿名

## 5. 用户数据隔离改造(PR 4-5)

- [x] 5.1 `WatchlistRepository`/`IndexWatchlistRepository`/`TagRepository` 改为用户作用域(user_id 必填、SQL 层过滤),新增 System 作用域查询(跨用户 DISTINCT instrument_id)
- [x] 5.2 `WatchlistService`/`IndexWatchlistService`/`TagService` 构造接收 CurrentUser 注入的 user_id;业务 API 全部接入
- [x] 5.3 标签业务逻辑适配用户命名空间:用户内唯一、跨用户同名允许、他人 tag_id 返回 404、绑定校验 tag 属主
- [x] 5.4 后台刷新与缓存预热改用 System 作用域去重集合(`app/main.py` 及相关 Job)
- [x] 5.5 数据隔离集成测试(A/B 双用户):列表互不可见、同时关注同一证券、同名标签、A 不可读/绑 B 的 tag、排序互不影响、A 删除不影响 B、admin 个人数据同样隔离、多用户去重刷新

## 6. 数据库迁移(PR 6)

- [x] 6.1 编写 Alembic 迁移 `0002_multi_user_auth`:建 seq_user_id/app_user/user_session → 4 张 v2 表 → legacy owner(admin,占位哈希,must_change_password=true)→ 拷贝归属 → 行数/外键校验 → 删旧表 → 改名
- [x] 6.2 迁移测试(真实临时 DuckDB):旧 schema+测试数据 → upgrade head → 验证归属、行数、标签关系、外键、全局表行数不变、校验失败回滚
- [x] 6.3 验证启动时自动迁移对旧库升级生效;启动日志/文档提示运行 CLI 设置 admin 密码

## 7. 管理员用户管理(PR 7)

- [x] 7.1 实现 `app/api/admin_users.py`:GET/POST `/api/admin/users`、PATCH `/api/admin/users/{id}`、POST reset-password;不提供 DELETE
- [x] 7.2 实现 `/admin/users` 管理页面(列表/创建/启停/角色/重置密码),风格与既有 Jinja2 页面一致
- [x] 7.3 集成测试:用户 CRUD 状态码、禁用后 Session 失效、重置密码后 Session 失效、user 访问 403、无 password_hash 泄露

## 8. 管理 CLI(PR 7)

- [x] 8.1 实现 `python -m app.cli users set-password/create/promote`(终端安全输入密码,不进 shell history)
- [x] 8.2 CLI 测试与使用文档(README/部署说明:升级后设置 admin 密码、新部署创建首个管理员)

## 9. CSRF 与安全增强(PR 8)

- [x] 9.1 实现 CSRF:Session 关联 csrf_token、写方法统一校验 `X-CSRF-Token`(登录除外)、Jinja2 meta 注入、前端 fetch 统一带头
- [x] 9.2 Cookie 安全属性:HttpOnly/SameSite=Lax/Path=/,`Secure` 随配置(生产开启);日志不输出密码/Token
- [x] 9.3 CSRF 集成测试:缺失/错误 Token 403、正确 Token 通过、GET 不要求
- [x] 9.4 登录限速集成测试(429、窗口外恢复)

## 10. 回归与收尾

- [x] 10.1 全量存量测试(行情/基本面/后台任务/标签/自选)在登录态 fixture 下通过,无退化
- [x] 10.2 更新 pyproject/README/部署文档:新依赖、认证配置项(session.ttl_days、cookie Secure)、升级步骤(备份→自动迁移→CLI 设密码)
- [x] 10.3 运行完整测试套件与 lint,按阶段分组提交(git 分批提交,沿用仓库提交规范)
