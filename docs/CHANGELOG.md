# Changelog

本文件记录 marketmind 的版本演进。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号从 v0.1.0 重新起步（marketmind 是以 stocksview 架构为基础的 DuckDB 演进版，
不继承 stocksview 的 SQLite 版本历史）。

## [v0.3.1] - 2026-09-20

修复 A 股历史首次回填因 Tushare 历史证券代码变更而无法推进
（OpenSpec 变更 fix-tushare-ts-code-alias）。

### 修复

- **历史 ts_code 别名规范化**（`app/providers/history/tushare_aliases.py`）：
  Tushare 历史事实接口返回的是证券**当时的代码**，而 `stock_basic` 只含当前
  代码。深赤湾A `000022.SZ` 于 2018-12-26 变更为招商港口 `001872.SZ` 后，
  2010 年的 daily / adj_factor / daily_basic / moneyflow 仍以旧代码返回，
  映射 `instrument_id` 必然失败——而旧代码在任何 `exchange × list_status`
  分片都不存在（主档已取全部 15 片），§35.1 的"刷新一次主档"恢复路径结构性
  无效。线上表现：`daily` / `adj_factor` / `daily_basic` 卡在 2010-01-04，
  `moneyflow` 卡在 2010-01-05，水位永不推进
- 修复方式：Provider 边界的已登记别名层（`TUSHARE_TS_CODE_ALIASES`，首条
  `000022.SZ -> 001872.SZ`），在结构检查之后、`ts_code → instrument_id`
  映射之前改写；四个日级数据集与主路径 / 逐证券 fallback 两条抓取路径统一
  生效；`raw_row_count` 仍在改写前取值，监控口径不变
- 新旧代码并存且业务字段一致时只保留规范代码行（WARNING 含
  `action=drop_legacy`）；字段冲突时抛 `ALIAS_CONFLICT`（新增错误码，
  归入配置类错误：首次尝试即终态失败、水位不推进），由人工用权威来源判定，
  不静默择一
- 严格保护不变：未登记的未知代码仍 `UNKNOWN_INSTRUMENT`，不按后缀/代码段
  推断别名，不改写主档三个数据集，不创建占位证券，`DUPLICATE_KEY` 与
  `known_instrument_ids` 校验不削弱
- 两步别名链（`A→C` 与 `B→C` 都已登记，同日返回 A 与 B）按组内是否含字面
  规范行分别选取参考行后比较合并；不处理会退化成 `DUPLICATE_KEY`——既不
  检测真冲突，又要白等重试
- 无数据库迁移、无数据格式变更；影响仅限后续抓取
- 新增 `scripts/spike/verify_ts_code_alias_online.py`：上线前只读在线验证
  （不打印 Token、不写数据库），确认旧代码在四个 endpoint 上的真实返回形态；
  规范代码不在主档时给出「停止部署」结论

### 已知限制

- **主档同时含新旧两码时 `daily_basic` 截断补齐会撞 `DUPLICATE_KEY`**：
  候选集来自 `cn_stock_basic`、Provider 输出经别名改写成规范代码，两侧
  身份口径不一致。线上实测主档只含新代码，此路径不可达；若在线验证脚本
  报告旧代码仍在 `stock_basic` 分片中，需先评估 Service 层改动再部署
  （详见 OpenSpec design 的 Known Limitations）

## [v0.3.0] - 2026-09-18

A 股历史数据同步（OpenSpec 变更 a-share-historical-data）。新增 Tushare 历史数据
Provider、四张日级事实表与三张证券主档表，后台 Job 按交易日推进并保证单日原子替换；
管理员在 `/admin/data` 查看进度、手动触发补齐。

### 新增

- 数据源：Tushare 历史接口 Provider（`daily` / `adj_factor` / `daily_basic` /
  `moneyflow` / `stock_basic` / `trade_cal` / `namechange` / `stock_company`），
  统一内部标准模型；进程级共享请求 gate（默认 0.6 秒/请求，`stock_basic` 1.25 秒）
- 数据表：四张日级事实表 `market_daily_bar` / `market_adj_factor` /
  `market_daily_basic` / `market_moneyflow`（`(instrument_id, trade_date)` 主键，
  按日期整日替换）；三张主档表 `cn_stock_basic` / `cn_stock_company` /
  `cn_stock_name_change`（覆盖沪深北三市场，含退市证券，不裁剪到 2010 年起）；
  状态库 `history_sync_state` / `history_sync_run` / `history_sync_run_dataset` /
  `history_day_status`
- 同步编排（`app/services/history/`）：单日原子事务（查旧行数 → 整日 DELETE →
  批量 INSERT → 更新日状态 → 推进水位 → 提交，全过程在写锁内且不做网络请求）；
  指数退避重试（默认 5s 起、最大 300s、20% 抖动）；数据集相互独立推进；
  交易日历严格模式（`source='tushare'` 缺失即失败，不用工作日兜底）；
  可用时间 cutoff（北京时间 09:30/16:30/17:30/20:30，未到点记 WAITING_SOURCE 不推进）
- 完整性防护（技术方案 §33/§34/§35）：返回恰 6000 行判为 `TRUNCATION_RISK`，
  转逐证券细粒度请求后合并去重复检；未知证券刷新一次主档后重映射，
  仍未知则报 `UNKNOWN_INSTRUMENT` 且不推进水位（不建占位证券）
- 事实表批量写入经 DuckDB 注册视图 + `INSERT ... SELECT`（技术方案 §74 基准后
  优化）：6000 行单日替换由约 4.9 s 降至约 1.2 s（本机 synthetic 基准 497 s →
  124 s）；写入取自 `session.connection()` 的事务内连接，不新开连接，
  单日原子事务（DELETE + INSERT + 日状态 + 水位）语义不变
- 失败与恢复：错误码 15 类（`TUSHARE_TOKEN_MISSING` ~ `INTERNAL_ERROR`）；
  启动恢复把残留 RUNNING 运行标记 `INTERRUPTED`；进程级 single-flight
- 后台任务 `HistorySyncJob`：每日 `schedule_time`（默认 20:30 北京时间，
  含周末调用但日历判定不产生虚假交易日）触发，`startup_catchup` 控制启动补齐；
  进程退出时中断并落盘状态
- 管理员 API（`/api/admin/history-data/*`）：`GET /summary`（整体状态与各数据集
  水位，只读缓存日历、不发上游请求）、`POST /sync`（202 + `run_id`，
  运行中返回 409 并附当前 `run_id`）、`GET /runs`、`GET /runs/{run_id}`；
  `requested_by_user_id` 由服务端从登录态取得，不接受客户端传入
- 管理员页面 `/admin/data`：整体状态卡、四个日级数据集卡、主档状态表、
  当前任务进度、最近 20 次执行记录；运行中每 4 秒轮询、结束自动停止；
  单一"检查并更新数据"按钮（无补缺口/增量/重同步模式选择，无历史数据手工编辑）
- 配置 `history.*`（技术方案 §57）：起点 `2010-01-01`、调度时间、重试参数、
  请求节奏、主档刷新周期、各数据集 cutoff；全部字段可省略
- 迁移 `0003_a_share_historical_data`：11 张新表 + 索引，旧数据无损

### 变更

- 交易日历 Provider 支持严格模式（`strict=True`）：只认 Tushare 权威日历，
  缺失时报 `CalendarUnavailableError` 而不是回退工作日推断（历史同步依赖此项，
  既有估值/会话功能默认行为不变）
- 运行时依赖新增 `pandas`（Tushare 返回 DataFrame 的字段规整；历史事实表批量写入
  的 DuckDB 注册视图——`register` 只接受 DataFrame/Arrow/ndarray，不接受 dict）

### 说明

- 首次回填 2010 年至今可能跨越多次运行；限流下每轮推进有限，属预期行为，
  可在 `/admin/data` 观察进度，重复点击不会重复执行（single-flight）
- 升级前建议停服备份 `data/marketmind.duckdb`（DuckDB 为单文件单写者）
- 不新建平行 Provider 框架、不引入 Redis / Celery、不改动 `fundamental_snapshot` 语义

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
