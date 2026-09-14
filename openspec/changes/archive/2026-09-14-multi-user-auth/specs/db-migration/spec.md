# db-migration Specification(Delta)

## MODIFIED Requirements

### Requirement: Alembic 版本管理

系统 SHALL 使用 Alembic 管理数据库结构:提供 `alembic.ini` 与 `alembic/versions/` 迁移目录;版本链以全新基线 `0001_duckdb_baseline` 开始,后续以 `0002_multi_user_auth` 引入多用户表与用户私有表改造;SHALL NOT 继承 stocksview 的 SQLite 迁移历史(0001_v002_baseline、0002_v003、0003_v003b 均废弃,不移植、不 stamp)。迁移配置 SHALL 复用应用配置的 database.url 与模型 metadata。

#### Scenario: 空库全链升级
- **WHEN** 对空数据库执行 `alembic upgrade head`
- **THEN** 建立全部核心表(instrument、quote_snapshot、fundamental_snapshot、trading_calendar、job_status、app_setting、watchlist、index_watchlist、tag、watchlist_tag、app_user、user_session)与 `seq_tag_id`、`seq_user_id` sequence,alembic_version 记录最新版本

#### Scenario: 迁移与模型一致
- **WHEN** `alembic upgrade head` 完成后比对模型 metadata 建表产物
- **THEN** 两者表集合、列与约束一致,无 schema 漂移

## ADDED Requirements

### Requirement: 多用户数据迁移

`0002_multi_user_auth` 迁移 SHALL 完成:创建 `seq_user_id` sequence、`app_user`、`user_session` 表;以"建 v2 表 → 拷贝数据 → 校验 → 删旧表 → 改名"流程重建 `watchlist`、`index_watchlist`、`tag`、`watchlist_tag`(复合主键含 `user_id`);创建 legacy owner(username `admin`、role `admin`、不可登录的占位密码哈希、`must_change_password=true`);把旧单用户数据全部归属 legacy owner。迁移文件与配置 SHALL NOT 包含任何明文默认密码。

#### Scenario: 旧库升级归属 legacy owner
- **WHEN** 对含旧单用户数据(自选/指数/标签/关联)的数据库执行 `alembic upgrade head`
- **THEN** 全部旧数据完整归属 legacy owner(用户 admin),legacy owner 登录后可看到原有自选与标签

#### Scenario: 全局表不受迁移影响
- **WHEN** 多用户迁移执行前后统计 instrument/quote_snapshot/fundamental_snapshot/trading_calendar 行数
- **THEN** 行数不变

#### Scenario: 迁移后无明文密码
- **WHEN** 检查迁移产物中 app_user.password_hash
- **THEN** 仅为不可登录的占位哈希值,迁移与配置文件中不存在明文密码

### Requirement: 迁移数据校验

多用户迁移 SHALL 内置校验并在失败时整体回滚:新旧表行数一致(watchlist、index_watchlist、tag、watchlist_tag);所有新行 `user_id` 均存在于 app_user;所有 watchlist_tag 关联指向同一用户的 watchlist;标签关联关系完整保留。

#### Scenario: 行数校验
- **WHEN** 迁移拷贝数据后
- **THEN** 四张私有表的旧表行数 == 新表归属 legacy owner 的行数,不一致则迁移失败回滚

#### Scenario: 校验失败回滚
- **WHEN** 校验发现行数不一致或外键悬空
- **THEN** 迁移整体回滚,数据库保持 0001 版本结构,不残留半成品 v2 表

### Requirement: 升级部署流程

数据库升级 SHALL 由现有"容器启动先执行 `alembic upgrade head`,成功后才启动应用"机制自动完成,SHALL NOT 要求用户手动执行迁移;升级后 SHALL 提示(文档/日志)运行 `python -m app.cli users set-password admin` 完成 legacy owner 初始化。

#### Scenario: 旧版本容器升级
- **WHEN** 使用含 0001 数据的旧数据库启动新版本容器
- **THEN** 启动过程自动完成 0002 迁移,应用就绪,旧数据归属 legacy owner

#### Scenario: 迁移失败阻止启动
- **WHEN** 0002 迁移校验失败
- **THEN** 容器退出,uvicorn 不启动,数据库保持迁移前状态
