# config-management Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 统一配置文件

应用配置 SHALL 统一保存在 config.yaml（database、quote.refresh_seconds/stale_seconds、tushare.token、providers、logging），由 app/config.py 启动时读取并注入；database.url 取值形如 `duckdb:///./data/marketmind.duckdb`（config.example.yaml 示例与 DatabaseConfig.url 默认值同步）；业务代码 SHALL NOT 直接读取 YAML。默认刷新周期 SHALL 为 60 秒，stale 阈值 180 秒。

#### Scenario: 配置读取
- **WHEN** 应用启动
- **THEN** 各 Provider/Service 收到统一配置对象，刷新周期为 60 秒

### Requirement: Token 安全

config.yaml 含真实 Tushare Token 时 SHALL 列入 .gitignore 不提交；仓库 SHALL 只提交不含真实 Token 的 config.example.yaml；Token SHALL NOT 写入数据库；日志 SHALL NOT 输出 Token 或完整敏感配置；SHALL NOT 依赖 TUSHARE_TOKEN 环境变量或 .env。

#### Scenario: Token 读取
- **WHEN** config.yaml 配置了 tushare.token
- **THEN** Tushare Provider 从配置对象获得 Token

#### Scenario: 日志脱敏
- **WHEN** 记录配置错误日志
- **THEN** 日志不包含 Token 明文

#### Scenario: 缺少 Token
- **WHEN** 未配置 Token 启动
- **THEN** 应用可启动，Tushare 功能记录明确配置错误，其他功能正常

### Requirement: 日志规范

系统 SHALL 使用标准 logging，至少记录：应用启动、数据库初始化、行情刷新开始/完成、AKShare/Tushare 请求失败、单个证券解析失败、自选增删、后台任务异常。

#### Scenario: 刷新日志
- **WHEN** 一轮行情刷新完成
- **THEN** 日志记录开始与完成（含成功/失败统计）

### Requirement: 历史数据配置节

应用配置 SHALL 新增 `history` 节，全部经现有 AppConfig 启动时读取注入，业务代码 SHALL NOT 直接读取 YAML。配置项至少包含：`enabled`（默认 true）、`start_date`（默认 "2010-01-01"）、`schedule_time`（默认 "20:30"）、`startup_catchup`（默认 true）、`max_attempts`（默认 10）、`request_min_interval_seconds`（默认 0.6）、`backoff_initial_seconds`（默认 5）、`backoff_max_seconds`（默认 300）、`jitter_ratio`（默认 0.2）、`stock_basic_refresh_hours`（默认 24）、`master_refresh_days`（默认 7），以及集中定义各数据集可用时间的 `history.availability`（adj_factor "09:30"、daily "16:30"、daily_basic "17:30"、moneyflow "20:30"）。配置缺省时 SHALL 以默认值运行且应用正常启动；市场时间基准 SHALL 统一复用 `BUSINESS_TZ_NAME = "Asia/Shanghai"`，SHALL NOT 新增任意 timezone 配置；Tushare Token SHALL 继续仅从 `tushare.token` 读取，SHALL NOT 新增 Token 数据库表、Admin 编辑页、API 返回或环境变量第二来源。

#### Scenario: 缺省配置可启动

- **WHEN** config.yaml 无 history 节时启动应用
- **THEN** 历史功能按默认参数运行，应用正常启动

#### Scenario: 调度时间可配置

- **WHEN** config.yaml 配置 history.schedule_time 为 "21:00"
- **THEN** 定时历史同步按 21:00 Asia/Shanghai 执行

#### Scenario: 修改可用时间 cutoff

- **WHEN** history.availability.daily 配置为 "17:00"
- **THEN** daily 的 latest_expected_trade_date 按 17:00 截止时间计算，其余数据集不受影响

