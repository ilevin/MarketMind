# instrument-management Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: instrument_id 生成规则

系统 SHALL 使用 `market + asset_type + symbol` 生成全局唯一 `instrument_id`，格式为 `MARKET:ASSET_TYPE:SYMBOL`。

#### Scenario: A股股票
- **WHEN** symbol=600519, market=CN, asset_type=STOCK
- **THEN** instrument_id 为 `CN:STOCK:600519`

#### Scenario: 指数代码与股票代码同码不同类型
- **WHEN** symbol=000001, market=CN, asset_type=INDEX
- **THEN** instrument_id 为 `CN:INDEX:000001`，与 `CN:STOCK:000001` 不冲突

#### Scenario: 港股
- **WHEN** symbol=00700, market=HK, asset_type=STOCK
- **THEN** instrument_id 为 `HK:STOCK:00700`

### Requirement: instrument 存储

系统 SHALL 将证券基础信息保存到 DuckDB `instrument` 表（instrument_id 直接作为主键，不再使用自增代理 id），包含 instrument_id（UNIQUE）、symbol、name、market、asset_type、exchange（可空）、currency、is_active、created_at、updated_at。market 仅允许 CN/HK；asset_type 仅允许 STOCK/ETF/INDEX；currency 为 CNY/HKD。

#### Scenario: 首次保存
- **WHEN** 添加一个新证券
- **THEN** instrument 表插入一条记录，instrument_id 唯一

#### Scenario: 重复保存幂等
- **WHEN** 同一 instrument_id 再次写入
- **THEN** 不产生重复记录，名称等信息可更新

### Requirement: 名称自动识别

添加标的时系统 SHALL 通过 Provider 自动获取证券名称；名称无法识别时 SHALL 添加失败并返回明确错误，不允许保存未知证券。港股指数识别失败时，错误信息 SHALL 附带代码格式指引（字母缩写及常见指数代码示例）。

#### Scenario: 识别成功
- **WHEN** 添加 CN/STOCK/600519
- **THEN** 保存名称为"贵州茅台"

#### Scenario: 识别失败
- **WHEN** 添加的代码在数据源中不存在
- **THEN** 返回错误信息，不写入 watchlist 或 index_watchlist

#### Scenario: 港股指数代码错误的指引
- **WHEN** 添加 `HK/INDEX/HS2083`（无效代码）导致识别失败
- **THEN** 错误信息在「无法识别证券」之外附带港股指数代码指引（如 HSI/HSCEI/HSTECH）

### Requirement: 数据库自动初始化

应用启动流程 SHALL 自动完成数据库结构准备，无需用户手工执行 SQL：结构创建与升级由 Alembic 迁移负责（启动时执行 `alembic upgrade head`），SHALL NOT 依赖 `create_all` 作为生产建表机制；测试环境可继续使用 metadata 建表。

#### Scenario: 首次启动
- **WHEN** data 目录无数据库文件时启动应用
- **THEN** 启动流程经迁移自动建立数据库和全部表，服务正常启动

#### Scenario: 结构落后时自动升级
- **WHEN** 数据库结构落后于当前迁移版本时启动应用
- **THEN** 启动流程自动补齐结构至最新版本后启动服务

### Requirement: symbol 输入规范化

系统 SHALL 在添加证券时对 symbol 统一规范化：去除首尾空白并转为大写。港股指数代码为字母缩写（如 HSTECH），SHALL 大小写不敏感；规范化 SHALL 在自选唯一性判定之前完成，避免同一证券因大小写不同产生重复配置。

#### Scenario: 港股指数代码大小写不敏感
- **WHEN** 添加 `HK/INDEX/hstech`
- **THEN** 规范化为 `HK:INDEX:HSTECH`，名称识别与行情获取正常

#### Scenario: 大小写不同不产生重复配置
- **WHEN** 已有 `HK:INDEX:HSTECH` 时再添加 `HK/INDEX/hstech`
- **THEN** 返回 409 Conflict（重复添加），不产生新记录

#### Scenario: 含空白的代码
- **WHEN** 添加 symbol 为 `" 600519 "`
- **THEN** 规范化为 `"600519"` 后正常处理

