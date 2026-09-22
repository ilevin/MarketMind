## ADDED Requirements

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
