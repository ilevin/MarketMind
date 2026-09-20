## Why

MarketMind 目前只保存自选股的实时行情快照与少量估值字段（`fundamental_snapshot` serving cache），缺少支撑历史 K 线、历史估值、资金流分析、选股与回测的长期数据底座。已确认的产品方案与技术方案（《docs/MarketMind_历史行情数据产品设计与数据说明.md》v0.1、《docs/MarketMind_A股历史数据技术方案_v0.2.md》v0.2）要求：以 Tushare Pro 为数据源，从 2010-01-01 起建立一份**可自行追平、失败交易日绝不跳过、可观测、可恢复**的 A 股全市场原始事实数据层。本变更将两份方案落为 v0.3.0 第一阶段实现，重点是"原始事实数据完整、连续、可观测、可恢复"，而非上层分析功能。

## What Changes

- 新增 8 个数据集的获取与存储：日级事实 4 个（`daily`、`adj_factor`、`daily_basic`、`moneyflow`）+ 主档 4 个（`stock_basic`、`trade_cal`、`namechange`、`stock_company`）。字段按 Tushare 当前接口可获取口径**完整保存**（含可选字段，显式 `fields` 声明），原始单位（手/千元/万元）与 NULL 语义原样保留，不预存 qfq/hfq 派生数据。
- 新增**连续水位线同步引擎**：四个日级数据集各自独立维护 `latest_complete_trade_date`，严格按 Tushare 交易日历顺序逐日推进；某日失败最多重试 10 次（指数退避 + 随机抖动），失败不跳日、水位不推进，下次执行从失败日继续；单日"事实写入 + day ledger + 水位推进"在同一数据库事务内原子提交；整日 DELETE + 批量 INSERT 保证幂等与上游修订一致。
- 新增统一同步入口 `HistorySyncService`：每日定时（默认 20:30 Asia/Shanghai）、启动 catch-up 与管理员手动触发共用同一套逻辑；进程内 single-flight 互斥（运行中重复触发 409）；进程中断后启动恢复（stale RUNNING 标记 INTERRUPTED，从水位继续）。
- 扩展现有 Provider 框架（不建第二套）：在 `app/providers/base.py` 增加历史内部标准模型与 `HistoricalMarketDataProvider` Protocol；新增 `HistoryProviderRegistry`（复用 `AppConfig` / `ProviderMetricsRegistry` / `call_with_metrics`）与 `TushareHistoricalMarketDataProvider`（Tushare DataFrame 不越过 Provider 边界）；新增共享 Tushare transport（`TushareRequestGate` 全局请求节奏 + client factory，逐步供现有 Tushare Provider 共享）；扩展现有 `TushareTradingCalendarProvider` 的 strict 历史范围模式（历史同步严禁工作日 fallback）。
- 扩展证券主档：`stock_basic` 全上市状态（含退市）按 exchange × list_status 分片获取后 upsert `instrument` 与 `cn_stock_basic`（exchange 取自 Tushare 返回，不按代码首位推断）；退市证券仅置 `is_active=false`，历史数据与主档身份永久保留。
- 新增管理员"数据管理"控制台 `/admin/data` 与 `/api/admin/history-data/*` API：总体状态 + 四个日级数据集状态卡（水位/目标/落后天数/记录数/错误）+ 主档状态 + 当前任务进度（轮询）+ 最近执行记录 + 一个"检查并更新数据"按钮（与定时任务同一逻辑）。
- 新增数据库迁移（基于当前 head `0002_multi_user_auth` 创建 `0003_*`）：3 张主档表（ORM）、4 张日级事实表（SQLAlchemy Core，无 ORM 主键/FK）、4 张同步控制表（`history_sync_state` / `history_day_status` / `history_sync_run` / `history_sync_run_dataset`），并为 `trading_calendar` 增加可空列（exchange、pretrade_date、source、fetched_at）。
- 新增 `history.*` 配置节（启用开关、起点 2010-01-01、调度时间、重试/退避/限流参数、主档刷新周期、各数据集可用时间 cutoff）。
- 现有 `FundamentalRefreshJob`、`fundamental_snapshot`、实时行情/自选功能**保持不变**（与新历史任务并存，共享 Tushare 限流）。

第一阶段**不包含**（非破坏性，仅范围声明）：分钟/Tick/逐笔数据、ETF/指数历史行情、财务报表库、回测引擎、因子/技术指标预计算、复权 K 线事实表、管理员手工修改历史数据、公开的历史查询 API（`/api/history/*` 留待下一阶段）。无 BREAKING 变更。

## Capabilities

### New Capabilities

- `historical-data-storage`: 历史数据集的表结构、字段策略与采集元数据——8 个数据集的存储模型、原始单位/NULL 语义、"完整保存原始字段"原则、事实表业务唯一键 `(instrument_id, trade_date)`、record_count/日期范围维护、trading_calendar 扩展列。
- `history-provider`: 历史数据获取能力——`HistoricalMarketDataProvider` Protocol 与内部标准模型、`ProviderBatch`（含 truncation_risk）、`HistoryProviderRegistry` 选源、Tushare 全字段显式声明与 normalize（ts_code→instrument_id）、6000/4500 行截断防护与分片、`TushareRequestGate` 全局请求节奏。
- `historical-data-sync`: 同步引擎——独立连续水位线、严格日历推进、失败不跳日、10 次退避重试、单日原子事务、幂等整日替换、空结果/截断不推进、master 前置与刷新周期、run/run_dataset 执行记录、stale run 恢复、single-flight 互斥、availability policy（latest_expected_trade_date）、统一调度（20:30 + startup catch-up + 手动同源）、graceful shutdown。
- `admin-data-management`: 管理员数据控制台——`/admin/data` 页面与 `/api/admin/history-data/{summary,sync,runs,runs/{id}}` API、管理员权限与 CSRF、运行中 409、进度轮询、overall_status 计算（只读 sync 小表，不扫事实表）。

### Modified Capabilities

- `market-session`: `TushareTradingCalendarProvider` 增加 strict 历史范围读取模式（`strict=True` 禁止工作日 fallback、失败即失败），实时市场状态保持现有 fallback 行为。
- `instrument-management`: 新增 stock_basic 主档同步对 instrument 的 upsert 映射规则（`CN:STOCK:<symbol>`、exchange 取自 Tushare、退市仅置 is_active=false 不删除）。
- `config-management`: 新增 `history.*` 配置节（含 availability cutoff），全部经 AppConfig 注入；Token 仍仅从 config.yaml 读取。
- `job-status`: `history_sync` Job 以 job_name 接入 JobStatusService（高层健康），详细进度由 history_sync_* 表负责，职责分离。
- `db-migration`: 版本链新增 `0003_a_share_historical_data`（新表 + trading_calendar 增列），不破坏既有表与数据。
- `deployment`: 升级/首次部署流程增加历史数据相关说明与真实数据升级前备份 DuckDB 要求。
- `provider-metrics`: 历史 Provider 接入统一指标体系（tushare_history_* metrics key），并新增 Tushare 全局请求节奏要求（RequestGate 进程级限速）。

## Impact

- **数据库**：DuckDB 新增 11 张表（3 主档 + 4 事实 + 4 同步控制），`trading_calendar` 增 4 个可空列；四张事实表达千万行级，使用 Core 批量写、无逐行 ORM；管理员统计读 `history_sync_state` 小表而非 `COUNT(*)` 事实表。首次回填 2010→今约 4 数据集 × 数千交易日，跨多次运行完成。
- **Provider 层**：`app/providers/base.py`（扩展）、新增 `app/providers/history/`（registry + tushare 实现）、新增 `app/providers/tushare_common.py`（共享 transport，现有 Tushare Fundamental/Calendar Provider 渐进接入，业务契约不变）。
- **服务/任务层**：新增 `app/services/history/`（availability、validation、planner、sync_service）、`app/jobs/history_sync.py`（asyncio.to_thread + 单飞 + shutdown 取消）；接入 FastAPI lifespan 与 JobStatusService。
- **API/前端**：新增 `app/api/admin_history.py`、`app/schemas/history_admin.py`、`app/templates/admin_data.html` 与对应原生 JS（含 Admin 导航入口、CSRF、运行中轮询）；不新增前端框架/构建系统。
- **配置**：`config.example.yaml` 增加 `history` 节；未配置时按默认值运行。
- **测试**：默认 `pytest` 全离线（fake/mock Provider + 真实临时 DuckDB）；真实 Tushare 仅 `@pytest.mark.online` smoke；关键回归场景（中间日失败不跳过、事务回滚、重启恢复、6000 截断、single-flight、迁移前向兼容）必须有集成测试；新增本地 synthetic 性能基准（防逐行 ORM 退化）。
- **运维**：v0.3.0 升级在真实数据上执行前备份 `data/marketmind.duckdb`；首次回填期间页面持续展示进度，任务可跨多次启动完成；Tushare 2000 积分限流策略保守（默认 0.6s/请求，stock_basic ≥1.25s）。
