## Why

ETF 在 MarketMind 中已是"一等资产"：自选/行情首页完整支持 ETF 实时行情（`asset_type=ETF`、腾讯源 `cn_etf` 通道），v0.4.1 又为数据管理预留了「ETF数据」「ETF历史」两个占位页——但历史数据体系完全没有 ETF：`DatasetName` 枚举只有股票 8 个数据集（`app/models/history_sync.py:33-43`），同步 universe 明确过滤 `asset_type=='STOCK'`（`app/services/history/sync_service.py:726-789`），四张事实表无一行行级读取 API，复权/量化/回测/AI Agent 场景无从开展。《ETF 数据模块 PRD & 技术方案设计》（外部文档，存放于仓库外用户工作区 `../temp/ETF模块_PRD与技术方案设计.md`，v0.4.2）给出方案：**东方财富（经 AKShare）提供市场行情事实，Tushare fund_adj 提供复权因子，底层保存 raw + factor，上层动态生成 qfq/hfq 与量化特征**。本变更将该方案落为实现，并替换两个 ETF 占位页。

## What Changes

- **三张新表（0005 迁移，基于当前 head `0004_per_stock_history_sync`）**：`cn_etf_basic`（ETF 业务主档，参照 `cn_stock_basic` 模式：instrument_id 主键、ts_code、name、exchange、list_date/delist_date（均可空）等 + source/fetched_at）；`etf_daily`（东方财富原始日线：instrument_id、ts_code、trade_date、open/high/low/close、volume、amount、turnover_rate + source/fetched_at）；`etf_adj_factor`（Tushare 复权因子：instrument_id、ts_code、trade_date、adj_factor + source/fetched_at）。两张事实表沿用项目惯例：SQLAlchemy Core Table、无物理主键/外键/二级索引、业务唯一键 `(instrument_id, trade_date)` 由区间 DELETE+INSERT 替换保证。**与 PRD 的有意偏差**：不建 `etf_instrument` 表——`instrument` 表 `asset_type` 枚举已含 ETF（instrument_id 即 `CN:ETF:<symbol>`，String(64) 主键，stocksview 技术方案明确"不另加整数 id"），PRD 的 BIGINT instrument_id 与项目身份模型冲突；ETF 身份落 `instrument`，业务字段落 `cn_etf_basic`。
- **数据集枚举扩展**：`DatasetName` 8→11（新增 `etf_basic`（MASTER）/`etf_daily`、`etf_adj_factor`（DAILY_CONTIGUOUS）），同步注册点同步扩展：`DAY_LEVEL_DATASETS`、`HISTORY_FACT_TABLES`、Provider 的数据集→请求映射、`validate_batch` 的 `_HANDLERS`、管理 API 的数据集校验与展示名。
- **ETF Universe Provider（主档发现）**：经 AKShare 东方财富 ETF 列表接口获取当前上市 ETF，upsert `instrument`（`CN:ETF:<symbol>`）与 `cn_etf_basic`；本轮未出现的已有 ETF 仅置 `is_active=false`（不删除，同 stock_basic 退市语义）。universe 刷新是 run 内**非硬前置**：失败仅将 ETF 数据集段标 FAILED，不终止 Run、不影响股票数据集（东财接口可用性有实测风险，见 design）。
- **ETF Daily Provider**：经既有 `get_history_by_stock(etf_daily, instrument, start_date, end_date)` 单次区间请求东方财富 ETF 历史日线（AKShare 适配，SOURCE='eastmoney'），显式字段清洗、行数上限防护、进程级东财请求节奏 gate（新建，模式对齐 `TushareRequestGate`）；新内部标准模型 `EtfDailyBar`（frozen dataclass，不复用股票 `DailyBar`——字段口径不同）。
- **ETF Adjustment Provider**：Tushare `fund_adj` 接口按 `(ts_code, start_date, end_date)` 拉取复权因子，复用现有 `AdjFactor` 模型、`TushareTransport`/`TushareRequestGate`、异常分类与超时体系；经 `HistoryProviderRegistry` 按数据集选源（`providers.history.etf_daily` / `providers.history.etf_adj_factor`，registry 从单源改为数据集→源映射，股票 8 数据集仍固定 tushare 不变）。
- **同步引擎复用（不建 `etf_sync_state`）**：`etf_daily`、`etf_adj_factor` 完全复用 per-stock 水位引擎——`stock_sync_state` 以 `(dataset, instrument_id)` 为键天然容纳 ETF 数据集，`StockSyncExecutor` 三段事务、`RetryPolicy`（max_retries=3）、失败隔离、自动补偿、空结果语义、`recover_stale_runs` 全部沿用；universe 换为 `asset_type=='ETF'` 主档 + `cn_etf_basic` 生命周期映射（list_date 缺失保守取 `history.start_date`，delist_date 缺失取 target，ETF 无退市回补）。两个数据集水位独立推进；`etf_adj_factor` 同步失败不影响 `etf_daily`（PRD"两数据源独立"）。触发与股票共用同一 `HistorySyncService.run` 单一入口（定时/启动/手动），一次"检查并更新"完成全部数据集。
- **Quant API（新能力）**：`app/services/quant/` 新子包，`get_etf_daily(symbol, start_date, end_date, adjust='raw'|'qfq'|'hfq')` 统一查询——读 `etf_daily` 区间、按需 JOIN `etf_adj_factor` **动态计算**前复权/后复权价格（复权价不入库，符合既有"不存储派生复权数据"要求），qfq/hfq 需要的因子缺失日期明确报错并列出缺失日（不静默回退 raw、不截断返回）；REST 暴露 `GET /api/quant/etf/daily`（登录用户，参数 symbol/start/end/adjust）供 AI Agent 与回测工具调用。
- **管理页面落地（替换占位页）**：`/admin/data/etf` 改为 ETF 数据总览（etf_basic/etf_daily/etf_adj_factor 数据集卡片 + ETF universe 概况 + 复用现有"检查并更新"按钮与轮询）；`/admin/data/etf/history` 改为 ETF 个股历史页（参照 `/admin/data/stocks`：数据集切换、状态筛选、名称/代码搜索、100 条服务端分页、失败详情只读 modal）。管理 API：summary 新增 ETF 数据集分组（个股口径统计复用现有字段）；`/stocks` 端点 dataset 参数扩展接受 `etf_daily`/`etf_adj_factor`（JOIN 表按数据集分派到 `cn_etf_basic`）；`/runs`、`/tasks/{task_id}` 自然覆盖 ETF 数据集；overall_status 在 ETF 数据集启用时把 ETF 缺口纳入 LAGGING 判定。
- **可得性与配置**：`history.availability` 新增 `etf_daily`（默认 16:30，收盘后）与 `etf_adj_factor`（默认 09:30，对齐股票 adj_factor）cutoff；`providers.history` 新增 ETF 选源键与默认值；`history` 新增东财请求最小间隔与 ETF universe 刷新周期配置；ETF 历史起点复用 `history.start_date`（2010-01-01，首只 ETF 2004 上市但早期市场规模极小，需要更早数据的用户改配置即可）。`config.example.yaml` 同步更新。
- **在线 spike 前置**：`scripts/spike/verify_etf_sources_online.py` 实测东方财富 ETF 列表/历史接口可用性（本环境 2026-08-18 曾实测东财接口被远端断连，A股实时行情因此改走腾讯——见 `app/providers/quote/tencent.py:3-5` 模块注释）、字段口径与成交量单位、`fund_adj` 的积分门槛/返回字段/行数上限，结论固化进离线测试 fake 基线；spike 结论影响 provider 实现细节但不阻塞离线开发框架。
- **明确不做**（非目标）：ETF NAV/成分股/申赎数据、基金经理信息、全市场退市 ETF 自动回补、ETF 代码别名机制（东财列表即当前上市集合，ETF 代码无变更先例）、MA/RSI/MACD 等指标入库（研究层动态计算）、MCP/Agent 专用协议（REST 即可）、前端框架引入、事实表读取的通用股票查询 API（仅 ETF）。无 BREAKING 变更（只增不删；`/admin/data/etf` 两页从占位变实功能属预留语义的兑现）。

## Capabilities

### New Capabilities

- `etf-quant-api`: 统一 ETF 历史数据查询——`get_etf_daily(symbol, start, end, adjust=raw|qfq|hfq)` 读事实表并动态计算复权价，跨源（日线 × 复权因子）日期一致性处理，REST 端点 `/api/quant/etf/daily` 供量化研究/回测/AI Agent 调用。

### Modified Capabilities

- `instrument-management`: 新增 ETF 主档要求——universe 自动发现经 AKShare 东财列表 upsert `instrument`（`CN:ETF:<symbol>`）与 `cn_etf_basic`，ACTIVE/INACTIVE 语义对齐 stock_basic 退市处置（不删除、仅置 is_active=false）。
- `historical-data-storage`: 新增 `etf_daily`/`etf_adj_factor` 事实表与 `cn_etf_basic` 主档表要求（原始事实、source/fetched_at 元数据、无派生复权、原始单位不改）。
- `historical-data-sync`: 数据集范围扩展（etf_basic/etf_daily/etf_adj_factor）；ETF 数据集复用个股水位引擎、失败隔离与自动补偿；universe 刷新为 run 内非硬前置（失败不终止 Run）；dataset 常量清单与 ETF 生命周期边界；ETF 空结果语义。
- `history-provider`: 新增 ETF 数据源接入要求——东财日线 Provider（AKShare 适配、东财请求节奏 gate、行数上限防护）、Tushare fund_adj 复权因子 Provider（复用 transport/gate/异常体系）、Registry 按数据集选源、内部标准模型 `EtfDailyBar`、Provider 边界纪律（AKShare DataFrame 不越界）。
- `admin-data-management`: 移除两个 ETF 占位页要求，新增真实 ETF 数据页/ETF 历史页要求；summary 新增 ETF 数据集分组；`/stocks` 端点扩展接受 ETF 数据集；overall_status 纳入 ETF 缺口。
- `config-management`: `providers.history` 新增 ETF 选源键、`history.availability` 新增两个 ETF cutoff、东财请求间隔与 universe 刷新周期配置项。
- `db-migration`: 版本链新增 `0005_*`（三张新表建表 + 幂等校验 + 不动既有数据），防漂移测试同步更新。

## Impact

- **数据库**：新增 3 张表（`cn_etf_basic` 约 1 千行、`etf_daily`/`etf_adj_factor` 约 1000 ETF × 数千交易日），0005 迁移纯 DDL 建表、可重复执行并内置校验；既有 25 张业务表结构与数据不变（0005 后全库 28 张）；`stock_sync_state` 增行不增列（(dataset, instrument_id) 键空间扩展，约 +2000 行）。
- **Provider 层**：`app/providers/base.py` 新增 `EtfDailyBar` 模型与 ETF 数据集方法签名；`app/providers/history/` 新增东财 ETF 实现（AKShare 适配）与 `fund_adj` 扩展；`HistoryProviderRegistry`（`app/providers/history/__init__.py`）从单源改为数据集→源映射；`app/providers/tushare_common.py` 不动（复用 transport/gate/异常分类）；新建东财请求 gate（模式对齐 `TushareRequestGate`）。
- **服务层**：`app/services/history/sync_service.py` 扩展（ETF universe 前置段 + 两个 ETF 数据集段复用 `_sync_stock_dataset` 泛化）；`app/models/history_sync.py` 枚举扩展；`app/models/history_fact.py` 新表映射；`app/services/history/validation.py` 新增 ETF 专项规则；`app/services/history/planner.py` 复用（生命周期参数化）；新增 `app/services/quant/`（Quant API）；`app/repositories/` 新增 ETF 主档仓储与事实表读路径。
- **API/前端**：`app/api/admin_history.py`（summary ETF 分组、/stocks 数据集扩展）、新增 `app/api/quant.py`（ETF 查询端点）；`app/templates/admin_data_etf.html`、`admin_data_etf_history.html` 从占位改实功能；`app/static/app.js` 新增两个 page_id 分支（复用 chip/table/status-badge/modal/pagination 组件与 api()/esc() 工具）。
- **配置**：`config.example.yaml` 新增 providers.history ETF 选源键、availability 两项 cutoff、东财间隔与 universe 刷新周期；缺省按默认值运行（ETF 同步默认启用）。
- **测试**：迁移防漂移（`tests/integration/test_migrations.py` 的 EXPECTED_TABLES/HEAD_REVISION/head 比对）；ETF universe/水位/失败隔离/空结果/生命周期集成测试（临时 DuckDB + fake provider，参照 per-stock 测试模式）；Quant API 复权计算单测（qfq/hfq 基准日、因子缺失语义、跨源一致性）；管理 API/页面测试扩展；`@pytest.mark.online` spike/smoke 覆盖东财与 fund_adj 实测（默认 pytest 全离线）。
- **运维**：升级停服务 → 成对备份 `.duckdb`+`.wal` → 启动执行 0005 → 验证 `/health` 与两个 ETF 页面 → 触发首轮同步（ETF 全量回填约 1000 ETF × 2 数据集 × 每股一次区间请求，东财 gate 节流下预计分钟级~小时级）；回滚 = 恢复备份 + v0.4.1 镜像（0005 多出的表不破坏 v0.4.1 读写，但同步状态两套模型不一致，须走备份恢复）。
- **依赖**：`akshare>=1.14` 已是直接依赖（无需新增）；无新第三方依赖。
