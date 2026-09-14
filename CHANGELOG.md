# Changelog

本文件记录 marketmind 的版本演进。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号从 v0.1.0 重新起步（marketmind 是以 stocksview 架构为基础的 DuckDB 演进版，
不继承 stocksview 的 SQLite 版本历史）。

## [v0.2.0] - 2026-09-14

多用户支持与用户认证（OpenSpec 变更 multi-user-auth）。自选 / 指数配置 / 标签按用户
完全隔离，页面与业务 API 要求登录；市场数据（行情快照 / 估值 / 证券主数据）全局共享一份。

### 新增

- 认证：服务端 Session + HttpOnly Cookie（`marketmind_session`，数据库只存 SHA-256(token)），
  Cookie 属性 HttpOnly / SameSite=Lax / Path=/，`Secure` 随 `auth.session.cookie_secure` 配置；
  登录 / 退出 / 当前用户 / 修改密码 API（`/api/auth/*`），登录页 `/login`
- 密码存储：Argon2id（`pwdlib[argon2]`），占位哈希 `!unloginable-placeholder` 不可登录
- CSRF 防护：Session 绑定 `csrf_token`，写请求校验 `X-CSRF-Token` 头（登录接口豁免，
  匿名写请求由认证依赖返回 401），Jinja2 meta 注入 + 前端 fetch 统一带头
- 登录限速：进程内 IP+username 维度，5 次失败 / 300 秒窗口，429
- 用户管理：`/api/admin/users`（列表 / 创建 / 启停 / 角色 / 重置密码，无物理删除）+
  `/admin/users` 管理页；最后一个有效管理员不可禁用 / 降级（409）；
  禁用 / 改角色 / 重置密码均撤销该用户全部 Session
- 管理 CLI：`python -m app.cli users set-password / create [--admin] / promote`
  （getpass 终端安全输入，密码不进 shell history）
- 数据隔离：`watchlist` / `index_watchlist` / `tag` / `watchlist_tag` 改为
  (user_id, …) 复合主键并按用户过滤（Repository 构造 user_id 必填）；
  新增 `SystemWatchlistRepository` 供后台任务跨用户 DISTINCT 聚合，
  多用户关注同一证券时行情仍只刷新一次
- 迁移 `0002_multi_user_auth`：新增 `seq_user_id` / `app_user` / `user_session`，
  四张私有表经 staging 表重建（DuckDB 1.5.5 不支持 RENAME 被 FK 引用的表、
  也不支持 ALTER ADD FOREIGN KEY），旧单用户数据全部归属 legacy owner `admin`
  （占位哈希、`must_change_password=true`），迁移内置行数 / 归属 / 外键校验，
  校验失败整体回滚

### 变更

- 权限模型：业务 API 要求登录（401），页面未登录 302 → `/login`，
  `/api/admin/*` 要求 admin（403）；匿名可达仅 `/login`、`/health`、`/static/*`
- 标签唯一性范围从全库改为同一用户内（跨用户同名允许），查重仍在写锁内进行
- 用户名规则：3~32 字符 `[A-Za-z0-9_-]`，唯一性大小写不敏感
- 配置新增 `auth.session.ttl_days`（默认 7 天）与 `auth.session.cookie_secure`（默认 false）

### 升级步骤（v0.1.0 → v0.2.0）

1. 备份数据库文件 `data/marketmind.duckdb`
2. 部署新版本（容器启动自动执行 `alembic upgrade head`，迁移失败即退出不启动）
3. 临时停服后运行 `python -m app.cli users set-password admin` 设置管理员密码
   （CLI 与服务进程不能同时打开同一 DuckDB 文件——单写者约束，Docker 部署用
   `docker compose down` + `docker compose run --rm marketmind python -m app.cli ...`；
   迁移写入的占位哈希不可登录，此步完成前无人能登录）
4. 重启服务，登录后在「用户管理」页创建普通用户

## [v0.1.0] - 2026-09-13

## [v0.1.0] - 2026-09-13

首个版本。以 stocksview（v0.03.1，A 股/港股行情看板）的分层架构与全部产品能力为基础，
持久层从 SQLite 整体切换为 DuckDB，为历史行情存储与回测打地基。

### 新增

- DuckDB 持久层：`duckdb==1.5.5` + `duckdb-sqlalchemy==1.5.5.5`（精确锁定版本），
  数据库文件 `data/marketmind.duckdb`
- 全新 Alembic 基线 `0001_duckdb_baseline`：一次创建全部 10 张核心表与 `seq_tag_id` sequence
- v1 表结构：业务主键取代自增代理 id（`instrument_id` 主键、复合主键、quote_snapshot 一证券一行）、
  显式 sequence、全库 TIMESTAMPTZ aware 时间语义
- `WriteCoordinator` 写事务协调器：进程内锁序列化全部写事务 + 有限重试
  （DuckDB 为嵌入式单写者数据库，乐观并发下同表并发写会冲突）

### 变更

- 部署约束：uvicorn 固定 `--workers 1` 单 worker，后台任务与 Web 请求同进程
- `watchlist_tag` 外键不再使用 `ON DELETE CASCADE`（DuckDB 不支持级联删除），
  删除自选条目时由 Service 在写锁内两段提交先删标签关联、再删条目
  （DuckDB 1.5.5 的 FK 检查看不到同事务内已删的子表行）
- `tag.name` 不设数据库 UNIQUE 约束（DuckDB 1.5.5 中被 FK 引用的父表
  UNIQUE 列不可 UPDATE），重名校验由 TagService 写锁内查重保证，行为不变（409）
- 标签列表计数查询 `GROUP BY` 展开全部列（DuckDB 严格执行 SQL 标准，
  不容忍裸列）
- 自选排序 tie-break 从 `id` 改为 `(sort_order, created_at)`
- `quote_snapshot` 写入由 SELECT-then-UPDATE 改为原子 upsert（主键保证一证券一行）
- 测试体系改跑真实临时 DuckDB 文件（不再用 SQLite 内存库代替），新增共享 conftest

### 破坏性变更

- **旧 stocksview（SQLite 版）部署不能原地升级到本版本**：数据库文件格式不兼容，
  SQLite 历史数据导入工具属后续版本（T14）
- 生产环境不可多 worker / 多进程写同一数据库文件
