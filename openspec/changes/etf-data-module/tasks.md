## 1. 前置在线 spike（阻塞 Provider 实现细节定稿，不阻塞迁移/框架/离线开发）

- [x] 1.1 编写 `scripts/spike/verify_etf_sources_online.py`：实测东财 ETF 列表接口（`fund_etf_spot_em` 及备选）可用性与返回列清单（是否含市场列/上市日期列）；实测东财 ETF 历史接口 `fund_etf_hist_em` 可用性、返回列、成交量/成交额/换手率单位口径、`adjust=''` 不复权行为与超大区间行数（记录样本：正常 ETF 全区间、新上市 ETF、疑似退市 ETF）
- [x] 1.2 实测 Tushare `fund_adj`：积分门槛与无权限时错误形态、返回字段、**行覆盖语义**（每交易日一行 vs 仅除权事件日一行）、单次行数上限、停牌/退市 ETF 空结果行为（记录样本：510300 全区间、新上市 ETF、fund_adj 疑似不覆盖的 ETF）
- [x] 1.3 依据 spike 结论回答 design.md Open Questions 1/2/3/4 并回写结论：exchange 首选来源（显式市场列 vs 首位推断）、单位口径写入 Provider docstring 与文档、`factor_at` 实现分支（每日一行 vs forward-fill）、etf_daily/etf_adj_factor 截断阈值与 cutoff 默认值；结论固化进离线测试 fake 响应基线

## 2. 数据模型与 0005 迁移

- [x] 2.1 `app/models/history_market.py` 新增 `cn_etf_basic` ORM（instrument_id 主键、FK→instrument（约束名 `fk_cn_etf_basic_instrument`）、ts_code/symbol/name/exchange、list_date 与 delist_date DATE 可空（delist_date 对齐 cn_stock_basic 双列模式，V1 恒 NULL）、source/fetched_at/source_last_seen_at/sync_run_id），风格对齐 `cn_stock_basic`
- [x] 2.2 `app/models/history_fact.py` 新增 `etf_daily` 与 `etf_adj_factor` 两张 Core Table（无物理主键/外键/索引；etf_daily：instrument_id/ts_code/trade_date/open/high/low/close/volume(BigInteger)/amount/turnover_rate/source/fetched_at；etf_adj_factor：instrument_id/ts_code/trade_date/adj_factor(NOT NULL)/source/fetched_at），`HISTORY_FACT_TABLES` 映射加两数据集
- [x] 2.3 `app/models/history_sync.py` 的 `DatasetName` 枚举新增 `etf_basic`/`etf_daily`/`etf_adj_factor`；kind 在 sync_service 各调用点传递（无集中映射表），6.3 编排扩展处为 etf_basic 传 `DatasetKind.MASTER`、为 etf_daily/etf_adj_factor 传 `DatasetKind.DAILY_CONTIGUOUS`
- [x] 2.4 创建 `alembic/versions/0005_*.py`（基于实施时实际 head，当前 0004）：三张表建表、`_table_exists` 幂等跳过、EXISTING_TABLES 迁移前后行数校验、downgrade 逆序 drop；SHALL NOT 写入任何业务数据行、SHALL NOT 修改既有表
- [x] 2.5 `tests/integration/test_migrations.py`：`EXPECTED_TABLES` 25→28、`HEAD_REVISION` 更新为 0005，新增 0005 场景（v0.4.1 库升级既有数据无损且新表为空、全新库全链、重复执行幂等、校验失败回滚干净），`test_alembic_head_matches_models_schema` 防漂移通过

## 3. 配置扩展

- [x] 3.1 `app/config.py`：`HistoryConfig` 新增 `etf_enabled=True`、`etf_request_min_interval_seconds=0.5`、`etf_universe_refresh_hours=24`；`HistoryProviderConfig` 新增 `etf_daily='eastmoney'`、`etf_adj_factor='tushare'` 选源键；`HistoryAvailabilityConfig` 新增 `etf_daily='16:30'`、`etf_adj_factor='09:30'`；全部带默认值缺省可启动
- [x] 3.2 `config.example.yaml` 更新（providers.history 选源键、history 节 ETF 配置项与注释、availability 两项）；缺省配置启动测试确认 ETF 默认启用
- [x] 3.3 `tests/unit/` 配置加载单测：ETF 配置项默认值、显式配置覆盖、`etf_enabled=false` 读取（对齐现有 HistoryConfig 测试模式）

## 4. Provider 层：内部模型、东财日线与 fund_adj

- [x] 4.1 `app/providers/base.py` 新增 `EtfDailyBar` 与 `EtfUniverseRecord` frozen dataclass（复权因子复用 `AdjFactor`，SHALL NOT 复制第二份模型）；`HistoricalMarketDataProvider` Protocol 的 `get_history_by_stock` 扩展覆盖 ETF 数据集（dataset 实际注解为 str——更新 docstring 取值说明并扩展返回类型联合为 ProviderBatch[DailyBar | AdjFactor | DailyBasic | MoneyFlow | EtfDailyBar]，etf_adj_factor 复用 AdjFactor），新增 `get_etf_universe()` 方法声明
- [x] 4.2 新建 `app/providers/eastmoney_common.py`（或并入现有通用层）：`EastmoneyRequestGate`（进程级 Lock + monotonic 最小间隔，读 `history.etf_request_min_interval_seconds`）与 `EastmoneyProviderError` 异常体系（error_code：EASTMONEY_TIMEOUT（TimeoutError 子类）/EASTMONEY_API_ERROR/SCHEMA_MISMATCH/UNKNOWN_INSTRUMENT），模式对齐 `tushare_common.py`
- [x] 4.3 新建 `app/providers/history/eastmoney_etf.py` `EastmoneyEtfHistoryProvider`：延迟 import akshare、`fund_etf_hist_em` 不复权单次区间请求（按 spike 固化的参数与单位口径）、`safe_float` 口径清洗、行数上限 truncation_risk 防护、`get_etf_universe()` 列表获取、SOURCE='eastmoney'、接收 Service 传入 Instrument 快照不访问数据库
- [x] 4.4 `app/providers/history/tushare.py` 扩展 `etf_adj_factor`：`_STOCK_RANGE_DATASETS` 加 fund_adj endpoint 映射（fields：ts_code/trade_date/adj_factor）、行构建器对齐 `_build_adj_factor_row` 口径（纳入区间映射后自动流经既有别名规范化，初始零登记等价不改写）、ts_code 由 `cn_etf_basic` 主档构造
- [x] 4.5 `app/providers/history/__init__.py` Registry 按数据集选源改造：dataset→source 映射（股票 8 数据集→market_data 语义不变；etf_daily/etf_adj_factor 按各自配置键；etf_basic 复用 etf_daily 键指定的源）、eastmoney 源注册、双源单例、`call_with_metrics` 包装、metrics 键 `{source}_history_{dataset}`、超时分别取 akshare/tushare
- [x] 4.6 单测：FakeEastmoneyClient（预置 DataFrame 响应）覆盖东财 provider（区间参数透传、清洗、上限、异常分类与 gate 节流、universe 解析）；FakeTushareClient 加 fund_adj endpoint 覆盖 fund_adj provider（字段透传、行数、SCHEMA_MISMATCH）；registry 路由与 metrics 键断言、股票数据集路由回归断言

## 5. Universe 刷新与仓储层

- [x] 5.1 `app/repositories/` 新增 ETF 主档仓储（参照 `history_master.py` 模式）：`upsert_cn_etf_master`（同事务 upsert instrument（CN:ETF:<symbol> 映射、is_active、exchange 规则）与 cn_etf_basic、本轮未见 ETF 仅置 is_active=false）、`list_cn_etf_instruments`（market='CN' AND asset_type='ETF' 含 inactive）、`get_cn_etf_lifecycle_map`（ts_code/list_date/delist_date）
- [x] 5.2 `HistoryFactRepository` 复用验证：`replace_for_instrument_range` 经 `HISTORY_FACT_TABLES` 泛化到两张 ETF 事实表（区间 DELETE + staging INSERT，chunk 1000），`tests/integration/test_history_repositories.py` 扩展 ETF 场景（区间替换幂等、无重复行）
- [x] 5.3 集成测试：universe 刷新 upsert/退市保留（is_active=false 不删除）/退市 ETF 重新出现在列表后恢复 is_active=true 且历史事实与自选引用不变/重复刷新幂等/交易所映射（含 159xxx→SZSE）；cn_etf_basic.list_date NULL 落库不推算

## 6. 校验层与同步编排

- [x] 6.1 `app/services/history/validation.py`：`_HANDLERS` 新增 etf_daily 专项规则（OHLC 非负且 high>=max(open,close)、low<=min(open,close)、volume/amount/turnover_rate 非负、NULL 保留）与 etf_adj_factor 规则（每条 adj_factor>0）、`_DAY_LEVEL_DATASETS` 扩展、区间模式日期校验复用；新增对应单测（非法 OHLC 拒绝、adj_factor<=0 拒绝、0 行合法）
- [x] 6.2 `app/services/history/retry.py`：确认东财体系抛出的 SCHEMA_MISMATCH/UNKNOWN_INSTRUMENT 复用既有 `CONFIG_ERROR_CODES` 快速失败（`is_config_error` 按错误码判断、两码已在集合中，无需扩展集合），补东财错误码分类断言测试
- [x] 6.3 `app/services/history/sync_service.py` 编排扩展：etf_basic 周期刷新段（`etf_universe_refresh_hours` 周期未到跳过；刷新失败 → etf_basic/etf_daily/etf_adj_factor 三数据集本轮 FAILED、零请求、不终止 Run、股票段不受影响；universe 为空同处置）；`_sync_stock_dataset` 泛化为参数化循环（universe 取数与 lifecycle 映射按 dataset 分派 cn_stock_basic/cn_etf_basic）；`DAY_LEVEL_DATASETS` 扩展为 6（股票 4 在前，etf_daily、etf_adj_factor 在后）；`history.etf_enabled=false` 跳过 ETF 段含 universe 刷新；AvailabilityPolicy 读新 cutoff 键
- [x] 6.4 集成测试（临时 DuckDB + fake provider，扩展 `test_history_sync_service.py`）：ETF 水位推进与单调不下降、单 ETF 失败隔离（重试耗尽只失败自己、Run SUCCESS、task_failed_count 正确）、自动补偿（失败 ETF 下轮追平）、已追平零请求、空结果推进（停牌区间 0 行 records_fetched=0）、universe 刷新失败不终止 Run 且股票段统计正常、enabled=false 完全跳过、etf_daily 与 etf_adj_factor 水位独立、全部追平时 NOOP
- [x] 6.5 中断恢复与生命周期测试：`recover_stale_runs` 把 ETF running task 置 interrupted 且水位不动（补断言）；list_date NULL 回退 history.start_date、退市 ETF（inactive 无 delist_date）空结果追平后零请求

## 7. Quant API 与 REST 端点

- [x] 7.1 新建 `app/services/quant/etf_data.py`：`get_etf_daily(symbol, start_date, end_date, adjust)`——instrument 定位（未知代码明确报错）、事实表区间读路径（etf_daily/etf_adj_factor 行级 SELECT——项目首个事实表读路径，升序）、raw 直读、`factor_at(trade_date)` 单点（每日一行基线；spike 证实仅事件日有行则 forward-fill，只改此单点）、hfq=raw×factor_t、qfq=raw×factor_t÷factor_latest（全库最新因子）、跨源一致性校验（区间内日线有行而因子缺失 → 明确报错列出缺失日；因子全空 → "复权因子未同步"报错）、复权只作用于价格字段
- [x] 7.2 新建 `app/api/quant.py` 与 `app/schemas/quant.py`：`GET /api/quant/etf/daily?symbol=&start=&end=&adjust=`（登录用户即可、401 未登录、422 参数校验：symbol 六位数字/adjust 枚举/start≤end、响应 {symbol,name,ts_code,instrument_id,adjust,items[]}、因子缺失语义化错误、空区间 200 空 items）
- [x] 7.3 测试：单测复权计算（qfq/hfq 公式与基准、forward-fill 两分支、停牌日因子参与基准、因子缺失报错、raw 不受因子表影响）；集成测试 REST 权限矩阵（401/200/422）与响应序列化（时间与数值口径）

## 8. 管理 API 与前端

- [x] 8.1 `app/schemas/history_admin.py` 与 `app/api/admin_history.py`：summary 新增 `etf_universe` 块（active_count/total_count/last_refreshed_at/enabled）与 `etf_datasets[]`（三个条目：etf_basic 按主档条目结构、etf_daily/etf_adj_factor 按日级条目同构 schema 含个股口径统计 stock_count（证券总数，与股票条目共用字段名保持同构），只读小表聚合）；`master_datasets` 加 etf_basic；`history.etf_enabled=false` 时不返回统计；`GET /datasets` 加三个数据集元信息与 DISPLAY_NAMES（ETF基础信息/ETF日线行情/ETF复权因子）
- [x] 8.2 `/stocks` 端点扩展：dataset 白名单扩为 6 个日级数据集、JOIN 按 dataset 分派（cn_stock_basic/cn_etf_basic）、响应 schema 兼容、422 与权限测试更新；`overall_status` 判定扩展（etf 数据集级 FAILED→ERROR、ETF lagging→LAGGING、enabled=false 不参与）与分支断言
- [x] 8.3 `app/templates/admin_data_etf.html` 重写为 ETF 数据总览（universe 概况卡、etf_basic 主档卡、etf_daily/etf_adj_factor 数据集卡、当前任务进度、"检查并更新数据"按钮复用 POST /sync、未启用说明态），`app/static/app.js` 新增 `initAdminDataEtfPage` 分支（10 秒轮询沿用、复用 api()/esc() 与现有组件）
- [x] 8.4 `app/templates/admin_data_etf_history.html` 重写为 ETF 个股历史页（数据集 chip、状态筛选、搜索、100 条分页、失败行只读 modal），`app/static/app.js` 新增 `initAdminDataEtfHistoryPage` 分支（结构复刻个股历史页逻辑）
- [x] 8.5 `tests/integration/test_admin_history_api.py` 与 `test_admin_data_page.py` 更新扩展：summary etf 分组与未启用行为、/stocks ETF 数据集分页筛选排序与 422、overall_status 四分支（ETF ERROR/LAGGING/关闭/HEALTHY）、两页面渲染与导航 active、权限矩阵 403/401 与 CSRF；既有占位页测试改写为真实页面测试

## 9. 全量回归与在线冒烟

- [x] 9.1 全量离线回归：`python -m pytest -m "not online" -q` 全绿（含受影响既有测试更新：migrations、provider、sync service、admin API/页面、config）
- [x] 9.2 `tests/integration/test_etf_online_smoke.py`（`@pytest.mark.online` 只读约定）：东财列表/历史真实请求断言（含 spike 样本 ETF 的空结果与区间行为）、fund_adj 真实请求断言（行覆盖语义与单位）、`get_etf_daily` raw/qfq/hfq 端到端抽样比对

## 10. 文档与发布

- [x] 10.1 更新 `docs/CHANGELOG.md`（v0.4.2 条目：三表、数据集、双数据源、水位复用、Quant API、两页面、配置项、升级注意——ETF 全量回填预期、fund_adj 积分依赖、qfq 基准滚动说明）与 `docs/README.md` ETF 章节（数据说明、单位口径、复权计算口径）
- [ ] 10.2 `app/version.py` 与 `pyproject.toml` 版本号升至 v0.4.2；最终校验（全量 pytest、`openspec validate`）并提交
