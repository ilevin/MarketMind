# database-persistence Specification(Delta)

## MODIFIED Requirements

### Requirement: 业务主键

核心表 SHALL 使用业务主键或复合主键:`instrument` 以 `instrument_id`(如 `cn:stock:600519`)为主键;`quote_snapshot` 以 `instrument_id` 为主键;`watchlist`、`index_watchlist` 以 `(user_id, instrument_id)` 为复合主键;`fundamental_snapshot` 以 `(instrument_id, trade_date)` 为复合主键;`trading_calendar` 以 `(market, trade_date)` 为复合主键;`watchlist_tag` 以 `(user_id, instrument_id, tag_id)` 为复合主键;`app_user` 以 `user_id` 为主键;`user_session` 以 `session_token_hash` 为主键。核心表 SHALL NOT 使用代理自增 id;`tag` 表的 `tag_id` SHALL 由显式 sequence(`seq_tag_id`)生成,`app_user` 的 `user_id` SHALL 由显式 sequence(`seq_user_id`)生成。

#### Scenario: tag_id 由 sequence 生成
- **WHEN** 先后创建两个标签
- **THEN** 两个 tag_id 均为由 `seq_tag_id` 生成的递增整数

#### Scenario: user_id 由 sequence 生成
- **WHEN** 先后创建两个用户
- **THEN** 两个 user_id 均为由 `seq_user_id` 生成的递增整数

#### Scenario: 复合主键保证幂等
- **WHEN** 同一 `instrument_id` 与 `trade_date` 的估值数据再次 upsert
- **THEN** fundamental_snapshot 中不产生重复行,已有行被更新

#### Scenario: 用户自选复合主键幂等
- **WHEN** 同一用户对同一 instrument_id 的 watchlist 行再次插入
- **THEN** 违反 `(user_id, instrument_id)` 复合主键,不产生重复行

#### Scenario: 跨用户同证券多行
- **WHEN** 用户 A 与 B 各自的 watchlist 各持有一条同一 instrument_id 记录
- **THEN** 两行共存,`(user_id, instrument_id)` 均唯一

## ADDED Requirements

### Requirement: 身份域表结构

`app_user` SHALL 含 user_id、username、password_hash、role(user/admin)、is_active、must_change_password、created_at、updated_at、last_login_at(nullable);`user_session` SHALL 含 session_token_hash(PK)、user_id(FK → app_user)、csrf_token、created_at、expires_at、revoked_at(nullable)。用户私有表(watchlist/index_watchlist/tag/watchlist_tag)的 `user_id` SHALL 外键关联 `app_user.user_id`;`watchlist_tag` SHALL 外键关联 `watchlist(user_id, instrument_id)`。

#### Scenario: session 表结构
- **WHEN** 检查 user_session 表
- **THEN** 含 session_token_hash 主键、user_id 外键、csrf_token、created_at、expires_at、revoked_at 列

#### Scenario: 私有表用户外键
- **WHEN** 向 watchlist 插入不存在的 user_id
- **THEN** 外键约束拒绝写入

### Requirement: Session 与登录写路径

Session 的创建、撤销、过期属于写操作,SHALL 经现有 `WriteCoordinator` 串行化提交;Session 校验(读)SHALL 走普通读路径。登录流程中的用户名查重、Session 创建 SHALL 在同一写锁内完成。

#### Scenario: 登录写事务走写协调器
- **WHEN** 并发多个登录请求
- **THEN** Session 创建由 WriteCoordinator 序列化提交,无写冲突异常抛出
