## ADDED Requirements

### Requirement: DuckDB 为唯一持久化数据库

系统 SHALL 以 DuckDB 为唯一持久化数据库，依赖 SHALL 锁定精确版本（`duckdb==1.5.5`、`duckdb-sqlalchemy==1.5.5.5`，不使用 `>=` 区间约束）；数据库连接 URL 形如 `duckdb:///./data/marketmind.duckdb`。创建 Engine 前 SHALL 自动确保数据库文件的父目录存在（`:memory:` 除外）。数据库连接层 SHALL NOT 包含任何 SQLite 专用逻辑（如 PRAGMA 语句、`check_same_thread` 等 connect_args、`.db` 路径处理）。

#### Scenario: 父目录不存在时自动创建
- **WHEN** 连接 URL 指向 `./data/marketmind.duckdb` 而宿主目录 `./data` 尚不存在
- **THEN** 创建 Engine 前自动创建父目录，随后成功建立连接并生成数据库文件

#### Scenario: 连接层无 SQLite 专用逻辑
- **WHEN** 检查数据库连接层代码（app/db.py）
- **THEN** 不存在任何 PRAGMA 语句、`check_same_thread` 等 SQLite 专用 connect_args 或 `.db` 路径处理逻辑

### Requirement: 单进程写入模型

生产部署 SHALL 单进程运行（uvicorn 以 `--workers 1` 启动，DuckDB 为嵌入式单写者数据库，SHALL NOT 多 worker / 多进程写同一数据库文件）；后台任务与 Web 请求 SHALL 共享同一 Engine，各自使用短生命周期的 Session。

#### Scenario: 单 worker 运行
- **WHEN** 生产环境启动应用
- **THEN** uvicorn 以 1 个 worker 运行，后台任务与 Web 请求处理在同一进程内

#### Scenario: 共享 Engine 与短 Session
- **WHEN** 后台任务与 Web 请求并发访问数据库
- **THEN** 两者共用同一个全局 Engine，各自创建独立的短生命周期 Session，Session 用完即关闭

### Requirement: 写事务协调

所有数据库写事务 SHALL 经进程内写协调器（WriteCoordinator）序列化提交；遇到写冲突 SHALL 有限重试（1~3 次）；数据库事务 SHALL 保持短小；网络请求 SHALL NOT 放在数据库事务内。

#### Scenario: 并发写序列化提交
- **WHEN** 两个线程同时提交对同一张表的写事务
- **THEN** 写事务由写协调器序列化提交，两个事务均成功，无写冲突异常抛出

#### Scenario: 写冲突有限重试
- **WHEN** 某写事务提交时发生 DuckDB 写冲突
- **THEN** 在有限次数（1~3 次）内自动重试提交，重试耗尽仍失败才向上抛出错误

#### Scenario: 事务内不含网络请求
- **WHEN** 写事务需要外部数据源（Provider 接口）数据
- **THEN** 网络请求在数据库事务开启前完成，事务内只包含数据库操作

### Requirement: 事务边界

Repository 层 SHALL 只 flush 不 commit，commit 由 Service/Job 层（调用方）负责。

#### Scenario: Repository 只 flush
- **WHEN** Repository 层写方法执行完毕
- **THEN** 变更仅 flush 到数据库连接，由调用它的 Service/Job 层显式 commit

### Requirement: quote_snapshot 单行语义

每只证券在 quote_snapshot 表中 SHALL 至多一行，由 `instrument_id` 主键保证；行情更新 SHALL 以 upsert 原地更新该行，SHALL NOT 产生同一证券的第二行。

#### Scenario: upsert 原地更新
- **WHEN** 同一 `instrument_id` 先后两次写入行情快照
- **THEN** 该证券在表中始终只有一行，行内容为最新一次写入的数据

#### Scenario: 最新行情直查
- **WHEN** 查询多只证券的最新行情
- **THEN** 按 `instrument_id IN (...)` 直查 quote_snapshot，每只证券至多返回一行，无需内存择新

### Requirement: 业务主键

核心表 SHALL 使用业务主键或复合主键：`instrument` 以 `instrument_id`（如 `cn:stock:600519`）为主键；`watchlist`、`index_watchlist`、`quote_snapshot` 以 `instrument_id` 为主键；`fundamental_snapshot` 以 `(instrument_id, trade_date)` 为复合主键；`trading_calendar` 以 `(market, trade_date)` 为复合主键；`watchlist_tag` 以 `(instrument_id, tag_id)` 为复合主键。核心表 SHALL NOT 使用代理自增 id；`tag` 表的 `tag_id` SHALL 由显式 sequence（`seq_tag_id`）生成，SHALL NOT 依赖 SQLite INTEGER PRIMARY KEY 自增。

#### Scenario: tag_id 由 sequence 生成
- **WHEN** 先后创建两个标签
- **THEN** 两个 tag_id 均为由 `seq_tag_id` 生成的递增整数

#### Scenario: 复合主键保证幂等
- **WHEN** 同一 `instrument_id` 与 `trade_date` 的估值数据再次 upsert
- **THEN** fundamental_snapshot 中不产生重复行，已有行被更新

### Requirement: 时间存储语义

时间列 SHALL 使用 TIMESTAMPTZ 存储 aware 时间值（写入 aware、读出 aware，时区保真往返）；交易日 SHALL 使用 DATE 类型；API 输出 SHALL 统一为北京时间 ISO 格式（带 +08:00）。

#### Scenario: aware 时间往返
- **WHEN** 向任意时间列写入 aware 北京时间值后读出
- **THEN** 读出值仍为 aware 且时刻与写入值一致，时区信息不丢失

#### Scenario: API 时间输出格式
- **WHEN** API 返回时间字段（如 /api/admin/status）
- **THEN** 时间为北京时间 ISO 格式并带 +08:00 时区偏移

### Requirement: 外键与删除行为

外键 SHALL NOT 使用 ON DELETE CASCADE（DuckDB 不支持级联删除）。删除自选条目时 SHALL 在同一写锁内先删除其标签关联（watchlist_tag）并提交、再删除条目（watchlist）并提交（DuckDB 1.5.5 的 FK 检查看不到同事务内已删的子表行，同事务先删关联再删条目会被误拦，见 design Open Questions 第 8 条；写锁保证两段之间无其他写者）；删除标签前 SHALL 检查引用计数，被自选引用时 SHALL 拒绝删除。

#### Scenario: 删除自选先删关联
- **WHEN** 删除一个带标签关联的自选条目
- **THEN** 该条目的 watchlist_tag 关联行先被删除并提交，随后 watchlist 行被删除并提交；删除完成后两者均已不存在（对外可观察行为与级联清理一致：关联消失、usage_count 递减）

#### Scenario: 删除被引用的标签被拒绝
- **WHEN** 删除一个仍被自选条目引用的标签
- **THEN** 删除被拒绝（409），标签及其关联保持不变

### Requirement: 健康检查数据库探测

`/health` 的数据库探测 SHALL 使用通用 SQL（`SELECT 1`），SHALL NOT 依赖特定数据库方言。

#### Scenario: 通用探测语句
- **WHEN** 请求 `/health` 触发数据库连通性探测
- **THEN** 以 `SELECT 1` 执行探测，数据库可连接时返回 `database: "ok"`，探测语句不含任何方言专有语法
