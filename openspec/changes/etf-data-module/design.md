# ETF 数据模块设计（v0.4.2）

## Context

MarketMind v0.4.1 现行架构（经 4 路并行代码审计确认）：

- **per-stock 水位引擎（v0.4.0 落地）**：`stock_sync_state` 以 `(dataset, instrument_id)` 为键维护个股水位，`StockSyncExecutor`（`app/services/history/stock_executor.py`）三段写锁事务（创建 sync_task=running → 锁外 fetch+validate → 原子提交"区间替换+水位推进+task 终态"），`HistorySyncService.run` 单一入口编排：创建 run → `recover_stale_runs` → 主档硬前置（trade_cal/stock_basic）→ 4 个日级数据集逐股同步 → `_finalize_run`。单股失败隔离、落后水位自动补偿、空结果推进水位、`RetryPolicy`（max_retries=3，总尝试 4）、`CONFIG_ERROR_CODES` 快速失败均已固化并有完整测试守护。
- **数据集注册点共六处**：`DatasetName` 枚举（`app/models/history_sync.py:33-43`，8 值）、`DAY_LEVEL_DATASETS`/`MASTER_DATASETS`（`sync_service.py:69-74`、`admin_history.py:100-108`）、`HISTORY_FACT_TABLES`（`app/models/history_fact.py:125-131`）、Provider 数据集→请求映射 `_STOCK_RANGE_DATASETS`（`app/providers/history/tushare.py:436-461`）、`validate_batch` 的 `_HANDLERS`/`_DAY_LEVEL_DATASETS`（`app/services/history/validation.py:394-405`）、管理 API 的 `DISPLAY_NAMES` 与 dataset 校验。
- **Provider 纪律**：`app/providers/base.py` 定义 `@runtime_checkable` Protocol + frozen dataclass（`DailyBar`/`AdjFactor`/`ProviderBatch[T]`），第三方 DataFrame/SDK 对象不得越过 Provider 边界；`HistoryProviderRegistry` 当前为**单源**（`providers.history.market_data='tushare'`，`_PROVIDERS` 仅一项）；Tushare 请求统一经 `TushareTransport`（`ts.pro_api(token, timeout=)`）+ 进程级 `TushareRequestGate`（默认 0.6s）+ `classify_tushare_exception` 异常归一化。
- **事实表写入**：`HistoryFactRepository.replace_for_instrument_range` 区间 DELETE + DuckDB 注册视图 INSERT SELECT（chunk 1000）；事实表/主档表元数据列为 `source`（小写：tushare/akshare/tencent）+ `fetched_at`（TIMESTAMPTZ）——事实与主档采集元数据不使用 ingested_at/updated_at 命名（`stock_sync_state` 等状态表的 `updated_at` 属行级时间戳，另一回事）。
- **读路径为零**：四张事实表在 `app/` 内除 count/max 统计外没有任何行级 SELECT；无量化/研究层（`research/` 仅是参考项目分析笔记）。
- **ETF 现状**：`instrument.asset_type` 枚举已含 ETF（instrument_id `CN:ETF:<symbol>`），当前 ETF instrument 行仅由用户添加自选产生；行情首页/自选页走腾讯实时行情（`cn_etf: tencent`）；`/admin/data/etf` 与 `/admin/data/etf/history` 占位页已就位（模板+导航+spec 固化）；`akshare>=1.14` 已是 pyproject 直接依赖，但现有唯一用法是实时行情 `ak.stock_zh_a_spot_tx()`，无任何历史接口封装；`fund_adj` 全仓库零引用。
- **关键环境事实**：本部署环境 2026-08-18 曾实测东财(*_em)/新浪接口被远端断连，A股实时行情因此改走腾讯通道（`app/providers/quote/tencent.py:3-5` 模块注释）——**东财 ETF 接口当前可用性必须经在线 spike 验证**。
- **约束**：DuckDB 1.5.5 单写者（uvicorn --workers 1）、全库不用 UNIQUE 约束/二级索引（写锁内 get-or-create 保证）、Alembic 链 head=`0004_per_stock_history_sync`、迁移防漂移测试（`EXPECTED_TABLES` 25 表、`test_alembic_head_matches_models_schema` 逐列比对）、默认 pytest 全离线（`-m 'not online'`）。

产品与技术方案见《ETF模块_PRD与技术方案设计.md》（外部文档，存放于仓库外 `../temp/` 目录，即用户工作区的 projects/temp/ 下）：东财（经 AKShare）出市场行情事实、Tushare `fund_adj` 出复权因子、底层保存 raw + factor、上层动态生成 qfq/hfq 与量化特征；复用水位引擎、不建 `etf_sync_state`。PRD 若干记法与项目现实有偏差（BIGINT instrument_id、`ingested_at` 命名、`market_data_sync_state` 表名），实现按项目现实校正，理由逐条记录于 Decisions。

## Goals / Non-Goals

**Goals:**

- ETF 成为历史数据体系一等资产：universe 自动发现、历史日线与复权因子全量回填 + 每日增量、原始事实不可变、来源可追溯（source 列）。
- 完全复用 per-stock 水位引擎——单 ETF 失败只影响自己，两数据集（日线/因子）水位独立推进，复权因子数据源故障不影响日线同步。
- 统一查询 `get_etf_daily(symbol, start, end, adjust=raw|qfq|hfq)` 动态计算复权价（复权价永不入库），供量化研究、回测、AI Agent（REST）使用。
- 两个 ETF 占位页落地为真实管理页（数据总览 + 个股历史），复用现有组件与 API 模式。
- 数据源可替换：Provider 经 registry 按数据集选源，股票数据集行为不变。
- 全部新行为有离线自动化测试；上游真实行为经 spike/smoke 实测后固化。
- v0.4.1 数据库可安全升级（0005 只增不删）。

**Non-Goals:**

- ETF NAV、成分股、申赎数据、基金经理信息、IOPV/跟踪指数展示（PRD V1 不支持）。
- 全市场退市 ETF 自动回补（universe 只含东财当前列表 + 已入库 ETF 置 inactive）。
- MA/RSI/MACD 等任何指标入库或指标计算服务（研究层自行计算）。
- ETF 代码别名登记扩展（ETF 无代码变更先例；别名层接入但初始零登记，机制可扩展）。
- MCP/Agent 专用协议、WebSocket、前端框架、构建链。
- 通用股票历史查询 API（Quant API V1 仅 ETF；股票复权查询另行演进）。
- 多进程并发同步、外部队列（沿用单写者纪律）。

## Decisions

### D1. ETF 身份：复用 `instrument`，不建 `etf_instrument`（PRD 校正）

PRD §4.1 的 `etf_instrument`（BIGINT instrument_id 自增）与项目身份模型直接冲突：项目证券身份主键是 `instrument.instrument_id` 字符串（`MARKET:ASSET_TYPE:SYMBOL`，`app/services/instrument_id.py`），`instrument.asset_type` 枚举本就含 ETF，stocksview 技术方案亦明确"不另加整数 id"。ETF 落地为：

- `instrument` 行：`instrument_id='CN:ETF:<symbol>'`（symbol 即 6 位 ETF 代码）、market='CN'、asset_type='ETF'、exchange='SSE'/'SZSE'、currency='CNY'、is_active——universe 刷新时 upsert。
- 新表 `cn_etf_basic`（业务主档，对齐 `cn_stock_basic` 模式）：instrument_id 主键（FK→instrument，迁移命名 `fk_cn_etf_basic_instrument`）+ ts_code（Tushare 口径 `510300.SH`）+ symbol + name + exchange + list_date（可空）+ delist_date（DATE 可空，对齐 cn_stock_basic 双列模式；V1 东财列表无退市日期来源、恒为 NULL，供 planner 生命周期边界参数完整）+ source/fetched_at/source_last_seen_at/sync_run_id。

理由：与 v0.4.0 D1（状态键用 instrument_id）同源——身份、水位、事实全部锚定同一 instrument_id；自选/行情页的 ETF instrument 行与历史数据自然衔接，同一 ETF 不出现两套身份。

### D2. 数据集命名与三处事实/主档表

新数据集常量（`DatasetName` 8→11）：

- `etf_basic`（MASTER 类：最近成功刷新语义，无水位）
- `etf_daily`（DAILY_CONTIGUOUS）
- `etf_adj_factor`（DAILY_CONTIGUOUS）

表名与数据集名一致（`HISTORY_FACT_TABLES` 映射直白）：

- `etf_daily`：instrument_id(String64)、ts_code(String16 冗余)、trade_date(Date)、open/high/low/close(Double)、volume(BigInteger)、amount(Double)、turnover_rate(Double)、source(String32 NOT NULL)、fetched_at(TIMESTAMPTZ NOT NULL)。Core Table、无物理主键/FK/索引——与四张股票事实表同惯例。
- `etf_adj_factor`：instrument_id、ts_code、trade_date、adj_factor(Double NOT NULL)、source、fetched_at。同上。
- PRD 的 `ingested_at`/`updated_at` 校正为项目惯例 `fetched_at`；PRD 的 DECIMAL/BIGINT 数值类型校正为项目事实表惯例（Double 为主、volume 用 BigInteger，对齐 `market_moneyflow.*_vol` 先例）。
- 原始单位原则沿用：东财成交量/成交额/换手率按接口原始单位原样保存（单位口径经 spike 固化后写入 Provider 文档与 Quant API 文档），单位换算只发生在展示/研究层。
- 换手率 PRD 要求保留（东财历史接口含"换手率"列）。

### D3. 六处注册点逐一扩展（新数据集接入清单）

1. `DatasetName` 枚举（`app/models/history_sync.py`）加 etf_basic/etf_daily/etf_adj_factor；`DatasetKind` 映射（etf_basic→MASTER，其余→DAILY_CONTIGUOUS）。
2. `DAY_LEVEL_DATASETS` 处理元组扩展为 6（股票 4 + ETF 2，处理顺序股票在前、ETF 在后）；`MASTER_DATASETS` 加 etf_basic。
3. `HISTORY_FACT_TABLES` 加 etf_daily→Table('etf_daily')、etf_adj_factor→Table('etf_adj_factor')。
4. Provider 映射：`_STOCK_RANGE_DATASETS`（tushare 实现内）加 `etf_adj_factor`（endpoint fund_adj）；东财 provider 定义自身 `etf_daily` 映射。
5. `validate_batch` 的 `_HANDLERS` 加两个 ETF handler + `_DAY_LEVEL_DATASETS` 扩展。
6. 管理 API `DISPLAY_NAMES`（etf_basic=ETF基础信息、etf_daily=ETF日线行情、etf_adj_factor=ETF复权因子）、`/stocks` 端点 dataset 白名单扩展、`GET /datasets` 元信息、summary 分组。

### D4. ETF Universe 刷新：run 内**非硬前置**，一次请求全量列表

- Provider 能力：经 AKShare 东方财富 ETF 列表接口（spike 首选 `fund_etf_spot_em`，失败备选其他东财列表接口）一次请求获取当前上市 ETF 全集（代码/名称/市场列）。实现为 `get_etf_universe() -> ProviderBatch[EtfUniverseRecord]`（新 frozen dataclass：symbol、name、exchange、list_date 可空）。
- 刷新语义：`etf_basic` 作为 run 内前置段，按 `history.etf_universe_refresh_hours`（默认 24h）周期刷新（对齐 `stock_basic_refresh_hours` 先例），非周期到达时跳过（数据集状态保持）。upsert `instrument` + `cn_etf_basic` 于同一写锁事务；本轮列表未出现的已有 ETF instrument 仅置 `is_active=false`（同 stock_basic 退市语义，不删除）。
- **非硬前置**：刷新失败（东财不可用）仅将 etf_basic 记 FAILED 并令本轮 etf_daily/etf_adj_factor 两数据集段记 FAILED（零请求），股票数据集段与 Run 整体不受影响、正常推进；Run 最终状态仍可为 SUCCESS（数据集级失败由 `history_sync_state` 与 overall_status 表达）。理由：股票主档（trade_cal/stock_basic）是股票数据集的正确性前提故为硬前置；etf_basic 失败只使 ETF 段无新鲜 universe，已有 universe 事实上仍可用，但为避免用陈旧 universe 制造缺口误判，本轮 ETF 段整体跳过为 FAILED 更诚实。东财可用性有实测风险，隔离失败域是本设计的关键取舍。
- exchange 映射：优先使用东财列表接口的显式市场列；接口无市场列时按 ETF 代码首位推断（5→SSE、1→SZSE）。与股票"不按首位推断"纪律的偏离理由：ETF 交易所映射无歧义且腾讯实时通道已用同一规则（`5xxxxx→sh、1xxxxx→sz`，`app/providers/quote/tencent.py`）；ts_code（Tushare 口径）由 symbol+后缀构造。spike 确认接口列后固化首选来源。

### D5. 东财 ETF 日线 Provider（AKShare 适配）

- 实现 `app/providers/history/eastmoney_etf.py`：`EastmoneyEtfHistoryProvider(config)`，提供 `get_history_by_stock(etf_daily, instrument, start_date, end_date) -> ProviderBatch[EtfDailyBar]`。
- AKShare 调用：`fund_etf_hist_em(symbol, period='daily', start_date=YYYYMMDD, end_date=YYYYMMDD, adjust='')`——**必须不复权**（adjust=''，保存原始 OHLC，符合"东财负责市场事实"与既有"不存储派生复权数据"要求）。延迟 import akshare（现有 `AkshareQuoteProvider` 模式）；单 ETF 一次区间请求，不逐日拆分。
- 内部标准模型 `EtfDailyBar`（frozen dataclass：instrument_id、ts_code、trade_date、open、high、low、close、volume、amount、turnover_rate）——不复用股票 `DailyBar`（其 vol/amount 为 Tushare 口径且有 pre_close/change/pct_chg/ah_* 字段，口径不同）。AKShare DataFrame 在 Provider 内清洗为 dataclass，不越界（对齐现有纪律）。
- 异常归一化：新建 `EastmoneyProviderError(error_code)` 体系（EASTMONEY_TIMEOUT=同时是 TimeoutError 子类、EASTMONEY_API_ERROR、SCHEMA_MISMATCH、UNKNOWN_INSTRUMENT），脏值清洗复用 `safe_values.safe_float` 口径；SCHEMA_MISMATCH/UNKNOWN_INSTRUMENT 经既有 `CONFIG_ERROR_CODES` 判定首试即终态（`is_config_error` 按错误码判断，两码已在集合中，无需为东财新增错误码），网络类可重试。
- 限流：新建进程级 `EastmoneyRequestGate`（模式对齐 `TushareRequestGate`：Lock + monotonic 最小间隔），默认间隔 `history.etf_request_min_interval_seconds=0.5`（spike 后可调）。独立于 Tushare gate——不同上游、不同限流模型（东财按 IP，无 token 配额）。全量回填约 1000 ETF × 2 数据集 ≈ 2000 请求，0.5s 节流下约 17 分钟，与股票回填（数小时）相比可忽略。
- 截断防护：单股 16 年约 4000 行，保留"返回行数 ≥ 6000 置 truncation_risk"防护（复用 `DAILY_ROW_CAP` 量级语义）对异常放大返回拒绝提交。
- SOURCE='eastmoney'（小写，对齐项目 source 列惯例；PRD 的 'EASTMONEY' 大写记法不采纳）。

### D6. Tushare fund_adj 复权因子 Provider

- 扩展现有 `TushareHistoricalMarketDataProvider`：`_STOCK_RANGE_DATASETS` 加 `'etf_adj_factor'`（endpoint `fund_adj`，fields `(ts_code, trade_date, adj_factor)`），行构建器复用 `_build_adj_factor_row` 口径、复用 `AdjFactor` 模型（字段同构）。
- 复用 `TushareTransport`/`TushareRequestGate`/超时/`classify_tushare_exception`/`call_with_metrics`（metrics 键 `tushare_history_etf_adj_factor`），零新基建。
- ts_code 由 instrument 主档（`cn_etf_basic.ts_code`）构造；响应映射复用 `_instrument_for_ts_code`（按 symbol 匹配）；etf_adj_factor 纳入 `_STOCK_RANGE_DATASETS` 后自动流经既有别名规范化 `normalize_historical_aliases`（别名层对含 ts_code 列的 DataFrame 通用，无数据集清单；初始零 ETF 登记，等价不改写；未来 ETF 代码变更可登记，机制免费获得一致性）。
- fund_adj 行为未知点（积分门槛、行数上限、**行覆盖语义**——每日一行 vs 仅除权事件日一行、退市/停牌空结果）全部由 spike 实测固化；行覆盖语义直接决定 D10 的复权计算方式，是 spike 的最高优先级问题。

### D7. Registry 从单源改为按数据集选源

`HistoryProviderRegistry`（`app/providers/history/__init__.py`）改造：

- `_DATASET_SOURCES: dict[DatasetName, str]` 由 config 构建：股票 8 数据集 → `providers.history.market_data`（默认 tushare，行为不变）；`etf_daily` → `providers.history.etf_daily`（默认 eastmoney）；`etf_adj_factor` → `providers.history.etf_adj_factor`（默认 tushare）；`etf_basic` → 复用 `etf_daily` 键指定的源（universe 与日线同源东财，不单设选源键）。
- 源注册表 `_PROVIDERS` 加 `eastmoney`；各源构造单例（tushare 单例 + eastmoney 单例），`get_history_by_stock` 按 dataset 路由到对应 provider 实例。
- metrics 键维持 `{source}_history_{dataset}` 既有规则（`eastmoney_history_etf_daily`、`tushare_history_etf_adj_factor`）；超时分别取 `providers.timeout.akshare`（东财/AKShare 通道）与 `providers.timeout.tushare`。
- 满足 PRD"数据源切换不影响上层业务、Provider 可替换"验收：Service/Executor 仍只依赖 Protocol 与 registry，不 import akshare/tushare。
- `get_etf_universe` 能力同样经 registry 暴露（etf_basic 数据集键路由到东财 provider），Service 不直接实例化。

### D8. 同步编排：股票段不变，ETF 段复用泛化的逐股循环

- `HistorySyncService.run` 顺序：主档硬前置 → 股票 4 数据集（不变）→ `history.etf_enabled`（默认 true）时：etf_basic 周期刷新（D4）→ etf_daily → etf_adj_factor。三种触发（SCHEDULED/MANUAL/STARTUP）共用同一入口不变。
- `_sync_stock_dataset` 泛化为数据集参数化循环（universe 取数、生命周期映射、provider 源随 dataset 走）：ETF 段 universe = `market=='CN' AND asset_type=='ETF'`（含 is_active=false）+ 生命周期取 `cn_etf_basic`（ts_code/list_date/delist_date）；股票段仍取 `cn_stock_basic`。批量补建 `stock_sync_state`、落后优先排序、逐股 planner 区间、Executor 三段事务全部零改动复用。
- `etf_enabled=false` 时 run 完全跳过 ETF 段（etf_basic 也不刷新），summary 无 etf_datasets、页面显示未启用——给东财持续不可用的部署一个明确的退出开关。
- 生命周期边界复用 `HistorySyncPlanner.stock_effective_range`：eff_start=max(history.start_date, list_date)（ETF list_date 缺失保守取 2010-01-01——东财列表若无上市日期列则以 NULL 入库，由 planner 回退，行为正确）；eff_end=min(target, delist_date)（ETF 几乎无 delist_date，取 target）。退市 ETF（列表消失置 inactive 但无 delist_date）持续向 target 请求，上游返回空结果合法推进水位后归零请求——正确且自愈。
- 空结果语义与股票一致：停牌/未交易 ETF 区间 0 行无异常 → 推进水位 records_fetched=0；fund_adj 对部分 ETF 不覆盖属合法空结果。spike/smoke 实测确认后固化 fake 基线。
- 可得性：`AvailabilityPolicy` 读 `history.availability` 新增键 `etf_daily`（默认 16:30，东财收盘后即可取）与 `etf_adj_factor`（默认 09:30，对齐股票 adj_factor 的盘前更新假设；spike 若发现 fund_adj 实际更新更晚则上调）。

### D9. 表结构细节与迁移 0005

- 迁移 `0005_etf_data_module.py`（基于 head 0004）：三张 `op.create_table`（`cn_etf_basic` 带 PK + `fk_cn_etf_basic_instrument`；两张事实表无约束）；`_table_exists` 幂等跳过；EXISTING_TABLES 迁移前后行数校验（纯增量）；downgrade 逆序 drop 三表（生产回滚走文件备份，同 0002/0003/0004 约定）。
- 防漂移测试同步：`EXPECTED_TABLES` 25→28、`HEAD_REVISION='0005_etf_data_module'`、`test_alembic_head_matches_models_schema` 自动比对新表。
- `stock_sync_state`/`sync_task`/`history_sync_state` 结构零改动：ETF 数据集行按需补建（每 (dataset, instrument_id) 一行，约 +2000 行）；`sync_task` 增长语义沿用（V1 接受无界）。

### D10. Quant API：动态复权与跨源一致性

新子包 `app/services/quant/`（`etf_data.py`），核心函数：

```python
get_etf_daily(symbol, start_date, end_date, adjust="raw") -> list[EtfDailyBar]
```

- raw：直读 `etf_daily` 区间行（升序）。
- qfq/hfq：读区间 `etf_daily` + 该 instrument 的 `etf_adj_factor`（含区间边界外的最新因子行——见基准规则），**在查询时计算，绝不落库**：`hfq_price = raw_price × factor_t`；`qfq_price = raw_price × factor_t / factor_latest`（factor_latest = 该 ETF 全库最新 adj_factor，与券商行情软件口径一致，当前价=真实价）。
- **因子覆盖语义**（spike 固化前的设计基线）：假设 fund_adj 每交易日一行（同股票 adj_factor）。若 spike 证实"仅除权事件日有行"，则改为"按 ≤t 的最近因子行 forward-fill"，公式不变、取因子方式变化——两种实现都封装在 `factor_at(trade_date)` 单点，spike 结论只改这一处。
- **跨源一致性（PRD §8 落地）**：区间内某交易日日线存在但因子缺失（etf_adj_factor 水位未追平或 fund_adj 不覆盖）→ qfq/hfq 请求**明确报错**并列出缺失日（不静默回退 raw、不用相邻日因子外推掩盖缺口）；因子存在而日线缺失（停牌日有因子行）→ 该日无行情输出，因子仍参与 factor_latest 基准。raw 查询不受因子表状态影响。
- 查询入口参数 symbol 为 6 位 ETF 代码（'510300'），内部经 `instrument`（CN:ETF:510300）定位；未知代码报明确错误。
- V1 不做区间行数硬限制（单 ETF 单年约 250 行，16 年约 4000 行，响应可控）；调用方文档写明单位口径。

### D11. REST 端点 `/api/quant/etf/daily`

- 新路由 `app/api/quant.py`：`GET /api/quant/etf/daily?symbol=&start=&end=&adjust=`，**登录用户**可用（非 admin 专属——研究用户场景），query 校验（symbol 必填、adjust ∈ raw|qfq|hfq、start≤end、日期格式）。
- 响应：`{symbol, name, ts_code, instrument_id, adjust, items: [{trade_date, open, high, low, close, volume, amount, turnover_rate}, …]}`；空区间返回空 items；复权因子缺失返回 422/409 语义化错误（含缺失日明细）。
- 该端点是 PRD 场景三（AI Agent）的落地：Agent/回测工具经 HTTP 获取标准序列，无需理解数据来源。复用现有 Pydantic schema 模式与登录依赖。

### D12. 管理页面与 API

- **`/admin/data/etf`（ETF 数据总览）**：模板 `admin_data_etf.html` 重写（page_id 不变，占位 spec 替换）——`etf_universe` 概况卡（ACTIVE ETF 数、最近 universe 刷新）、etf_basic 主档卡（最近刷新/状态）、etf_daily 与 etf_adj_factor 数据集卡（个股口径统计复用现有 summary 条目 schema：stock_count/up_to_date_count/lagging_count/today_*/completion_rate）、"检查并更新数据"按钮（复用现有 `POST /api/admin/history-data/sync` 与 10 秒轮询）、`history.etf_enabled=false` 时显示未启用说明。
- **`/admin/data/etf/history`（ETF 个股历史）**：模板 `admin_data_etf_history.html` 重写——结构复刻 `/admin/data/stocks`：数据集 chip 切换（etf_daily/etf_adj_factor）、状态筛选、名称/代码搜索、100 条服务端分页、失败行只读 modal（复用 `.chip`/`.table`/`.status-badge`/`.modal`/`.pagination` 组件与 `api()`/`esc()`）；`app/static/app.js` 新增 `initAdminDataEtfPage`/`initAdminDataEtfHistoryPage` 两个 `body[data-page]` 分支。
- **管理 API 扩展**（`app/api/admin_history.py`）：`summary` 响应新增 `etf_datasets[]`（三个条目：etf_basic 按主档数据集条目结构、etf_daily/etf_adj_factor 按日级条目同构 schema，供 ETF 页面三卡渲染）与 `etf_universe` 块，`master_datasets` 亦包含 etf_basic（/admin/data 主档表视图沿用）；`GET /stocks` 的 dataset 白名单扩为 6 个日级数据集，ETF 数据集时 JOIN `cn_etf_basic`（股票数据集仍 JOIN `cn_stock_basic`，按 dataset 分派 JOIN 目标，响应 schema 兼容）；`/runs`、`/runs/{run_id}`、`/tasks/{task_id}`、`GET /datasets` 天然覆盖（run 流水已按数据集记录）。
- **overall_status**：ETF 数据集纳入既有优先级链——ETF 数据集 FAILED → ERROR（东财系统性不可用值得 ERROR 级告警，页面同时展示股票部分健康）；ETF lagging → LAGGING；`etf_enabled=false` 时 ETF 完全不参与判定。股票数据集判定逻辑零改动。

### D13. 配置（`app/config.py` + `config.example.yaml`）

- `HistoryProviderConfig`：新增 `etf_daily='eastmoney'`、`etf_adj_factor='tushare'` 选源键（registry 按键构造，见 D7）。
- `HistoryConfig`：新增 `etf_enabled=True`（总开关）、`etf_request_min_interval_seconds=0.5`（东财 gate）、`etf_universe_refresh_hours=24`。
- `HistoryAvailabilityConfig`：新增 `etf_daily='16:30'`、`etf_adj_factor='09:30'`。
- ETF 历史起点复用 `history.start_date`（2010-01-01）：首只 ETF 2004 年上市但早期数量/规模极小、回测价值低，需要更早数据的用户改配置即可，不新增独立配置项。
- `providers.timeout.akshare=45` 沿用覆盖东财通道超时；缺省全部有默认值，升级零配置可跑。

### D14. 测试策略

- **离线单测**：东财 provider（FakeEastmoneyClient 预置 DataFrame 响应，模式对齐 FakeTushareClient——字段缺失/脏值/超时/上限分类断言）、fund_adj provider（FakeTushareClient 加 fund_adj endpoint）、Quant API 复权计算（qfq/hfq 公式、factor_latest 基准、因子缺失报错、forward-fill 两种语义）、availability/planner ETF 分支。
- **离线集成**（临时 DuckDB + fake provider，对齐 `test_history_sync_service.py` 模式）：universe 刷新 upsert 与 inactive 语义、ETF 水位推进/失败隔离/自动补偿/空结果、etf_basic 失败 → ETF 段 FAILED 且股票段与 Run 不受影响、enabled=false 跳过、迁移 0005 场景（v0.4.1 库升级无损/全新库/幂等重放）、管理 API（summary etf 分组、/stocks ETF dataset 分页筛选排序、422、403/401）与两页面渲染、`/api/quant/etf/daily` 权限矩阵与响应。
- **在线**（`@pytest.mark.online`，默认排除）：spike 脚本 `scripts/spike/verify_etf_sources_online.py`（东财列表/历史可用性、字段、单位、fund_adj 积分/字段/行覆盖语义/空结果）与 smoke 测试固化结论。
- 请求量：日常增量仅落后 ETF 发请求（零请求即零成本）；全量回填 ~2000 请求一次完成。

## Risks / Trade-offs

- [东财接口在本环境曾断连（2026-08 实测）] → spike 前置实测；registry 数据集选源使替换源只改 config+新 provider 类；`etf_enabled` 提供部署级退出开关；etf_basic 失败域隔离到 ETF 数据集段（不影响股票同步、不终止 Run）；东财 gate 限速防加剧封禁。
- [fund_adj 积分门槛未知，不足时复权因子全量失败] → 失败隔离使 etf_daily 与 raw 查询完全不受影响；qfq/hfq 按缺失日明确报错（不静默错算）；CHANGELOG 与页面文案明示"复权因子需 Tushare 相应积分"。
- [fund_adj 行覆盖语义未知（每日 vs 仅事件日）] → 复权取因子封装在 `factor_at` 单点，spike 结论只改一处；两种语义均有测试方案。
- [akshare>=1.14 未锁版，东财接口签名可能漂移] → AKShare 调用集中在 provider 单文件、延迟 import；在线 smoke 作为升级后回归手段；风险记录，不做锁版（锁版会错过上游修复，且本项目对 akshare 用法面窄）。
- [ETF 交易所按首位推断偏离股票纪律] → 仅在东财列表无显式市场列时使用（5→SSE/1→SZSE 无歧义，腾讯通道同规则先例）；spike 确认后固化首选来源并在 provider 文档记录。
- [东财单位口径（volume 手/股、amount 元/百元）未经实测] → "原始单位原样保存"原则下不构成正确性风险，但影响文档与展示层换算；spike 固化后写入 Provider docstring 与 Quant API 响应文档。
- [qfq 以全库最新因子为基准] → 与券商口径一致（当前价=真实价），但历史区间回测需注意基准随新因子滚动——文档明示；hfq 无此问题（复现性最强，回测首选）。
- [ETF universe 仅含当前上市（PRD 明示不做退市回补）] → 曾上市后退市的 ETF 若从未入库则历史缺失；已入库 ETF 退市后保留全部历史。接受为 V1 边界。
- [`sync_task` 无界增长 +~2000 行/轮] → 沿用 v0.4.0 已接受的语义，不新增清理策略。

## Migration Plan

1. 实施顺序（对应 tasks.md）：spike 在线实测 → 0005 迁移与模型 → Provider（东财/fund_adj）→ Registry 与同步编排 → 校验与 Quant API → 管理 API/页面 → 全量离线回归 → online smoke → 文档与版本号。
2. 部署：停服务 → 成对备份 `marketmind.duckdb` 与 `.wal` → 启动新镜像（容器 CMD 先 `alembic upgrade head` 执行 0005：纯建表，失败保持迁移前状态）→ 验证 `/health`、`/admin/data/etf` → 触发首轮同步观察 ETF 回填 → 放开定时。
3. 首轮同步：universe 刷新（1 请求）+ 全量回填约 2000 个区间请求（东财 gate 0.5s 节流约 17 分钟，fund_adj 走 tushare gate）跨轮自动续跑；`etf_daily` 与 `etf_adj_factor` 水位独立，因子数据源不可用时日线仍正常追平。
4. 回滚：停新版本 → 恢复升级前备份 → 启 v0.4.1 镜像。不支持 v0.4.1 直接运行在 0005 库上（多出的表不破坏 v0.4.1 读写，但两套同步状态语义不一致，须走备份恢复）；0005 downgrade 仅 DDL 逆操作，生产回滚以文件备份为准（同既有约定）。

## Open Questions

1. **东财 ETF 列表/历史接口当前可用性与字段**（fund_etf_spot_em 列清单——是否含市场/上市日期列；fund_etf_hist_em 列与单位）——spike 实测，决定 D4 的 exchange 来源与 D2 单位文档，不阻塞迁移/框架开发。
2. **fund_adj 的积分门槛、行数上限、行覆盖语义（每日一行 vs 仅除权日）**——spike 最高优先级，决定 D10 `factor_at` 的实现分支与 etf_adj_factor 的截断阈值。
3. **fund_adj 对停牌/退市 ETF 的空结果语义**——smoke 实测固化 fake 基线。
4. **etf_daily 的合理 cutoff**（东财收盘后数据就绪时间）与 **etf_adj_factor 的 cutoff**（fund_adj 实际更新时刻）——spike 观察，默认值 16:30/09:30 可在配置中调整。

## Open Questions 结论（2026-10-09 spike 实测回写，tasks 1.1~1.3）

运行 `scripts/spike/verify_etf_sources_online.py`（akshare 1.18.94，只读实测）：

**OQ1（列表/历史接口可用性与字段）**——结论分环境两态：

- 东财列表接口 `fund_etf_spot_em`（push2delay.eastmoney.com）：**当前 502 不可用**（2026-10-09 实测，与 Risks 节"2026-08 断连"一致）。
- 备选新浪列表接口 `fund_etf_category_sina(symbol='ETF基金')`：**可用**，1694 只上市 ETF；列含 代码/名称/最新价 等，**代码前缀 sz/sh 承载市场信息、无显式市场列、无上市日期列**。
- 东财历史接口 `fund_etf_hist_em`（push2his.eastmoney.com）：**本环境连接失败**（ConnectionError）；新浪历史接口 `fund_etf_hist_sina` 可用（510300 全历史 3489 行、升序、volume 单位为股、amount 元、**无换手率列**）——与东财列口径不同，仅作观察记录，不作实现依据。
- **裁决（D4 修正）**：universe 实现固化为 `fund_etf_category_sina`（新浪源——东财列表接口恢复后可评估切回，接口差异已在 provider docstring 记录）；exchange 首选来源 = **代码前缀解析（sz→SZSE / sh→SSE，与首位推断 5→SSE、1→SZSE 一致，无歧义）**；list_date 无来源列 → **保存 NULL**（planner 回退 history.start_date，已实现）。`fund_etf_hist_em` 的列/单位口径（成交量手、成交额元、换手率百分比数值、adjust='' 不复权）按 akshare 文档口径实现，**待东财可用环境由在线 smoke（tasks 9.2）最终确认**；东财不可用环境中 etf_daily 段将持续 FAILED（重试耗尽后），失败域隔离设计（D8）生效、股票段不受影响。
- **已知偏离记录**：provider 通道名与选源键沿用 'eastmoney'（registry/metrics/source 列），而 universe 列表的物理来源当前为新浪接口——通道标识与物理端点的偏离在 CHANGELOG 明示，东财列表恢复后切回即消除。

**OQ2（fund_adj 积分门槛/行数上限/行覆盖语义）**——**待有 Token 环境实测**：本环境无 `tushare.token`，仅固化无权限错误形态（HTTP code=40101、msg「您的token不对，请确认。」）。`factor_at` 实现分支维持「每日一行基线直接查表」（fund_adj 文档口径），**单点封装在 `EtfDataService._factor_at`**——若实测为仅事件日有行，只改该单点为 forward-fill（取 ≤ trade_date 的最近因子）+ 对应单测两分支（离线测试已按此预案留位）。

**OQ3（fund_adj 停牌/退市空结果语义）**——待有 Token 环境随 OQ2 一并实测；离线 fake 基线先按「无异常 0 行 = 合法空结果 → 推进水位」固化（与股票数据集同构语义，v0.4.0 spike 已证该口径在上游成立）。

**OQ4（cutoff 默认值）**——观察不足（东财历史通道不可达、fund_adj 无 Token），**维持默认 etf_daily=16:30 / etf_adj_factor=09:30**，两值均在配置可调（`history.availability.*`）；上线后按实际数据就绪时间调整。

