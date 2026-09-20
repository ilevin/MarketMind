# MarketMind A股历史数据技术方案

> 文档版本：v0.2  
> 文档状态：技术方案确认稿 / Claude Code 实施输入  
> 编制日期：2026-09-16  
> 上游产品文档：《MarketMind A股历史数据：产品设计与数据说明》v0.1  
> 目标项目：https://github.com/ilevin/MarketMind  
> 数据源：Tushare Pro  
> 历史日级数据基准起点：2010-01-01

---

# 1. 文档目的

本文把已经确认的产品需求落成一套可以直接用于 Claude Code 实施的工程方案。

技术方案重点回答：

- 数据库如何建模；
- 历史事实表、证券主档和现有 `instrument` 如何衔接；
- 四个日级数据集如何维护独立连续水位线；
- 如何保证“数据写成功后才能推进水位”；
- 如何做到失败日期绝不跳过；
- 如何在 DuckDB 单写者约束下高效批量落库；
- 如何处理 Tushare 限流、6000 行截断风险和空结果；
- 如何实现首次 2010→当前的全量回填和后续每日追平；
- 如何复用 MarketMind 现有 WriteCoordinator / JobStatus / FastAPI / Jinja2 结构；
- 管理员“数据管理”页面需要哪些 API、状态与交互；
- 如何实现任务互斥、进程重启恢复和幂等；
- 如何组织代码和测试；
- Claude Code 应按什么顺序实施。

本方案不改变已经确认的产品边界。

最核心的工程不变量为：

> **`daily`、`adj_factor`、`daily_basic`、`moneyflow` 各自拥有独立连续水位线。某数据集某个交易日未完整成功，该数据集绝不能越过该日期继续推进。**

同时增加一条实现约束：

> **历史数据获取必须扩展并复用 MarketMind 现有 Provider 框架：统一使用 `app/providers/base.py` 的内部标准模型/Protocol、Provider Registry、`AppConfig` 配置注入以及 `ProviderMetricsRegistry/call_with_metrics`。不得再建立一套平行的 Provider 基础设施。**

---

# 2. 现有 MarketMind 架构约束

截至本文编制时，MarketMind 当前采用：

- Python 3.11+；
- FastAPI；
- Jinja2 + 原生 JavaScript/CSS；
- SQLAlchemy 2.x；
- DuckDB + `duckdb-sqlalchemy`；
- Alembic 管理数据库迁移；
- Tushare 作为 A 股估值与交易日历数据源；
- 单应用进程；
- Uvicorn 固定 `--workers 1`；
- 应用内通过 `WriteCoordinator` 串行化全部数据库写事务；
- 后台 Job 与 Web API 位于同一进程；
- 自动化测试使用真实临时 DuckDB 文件，默认不依赖网络。

这些约束继续保留。

## 2.1 不新增的基础设施

第一阶段明确不引入：

- Redis；
- Celery；
- RabbitMQ；
- Kafka；
- PostgreSQL / MySQL；
- 独立任务服务；
- 新的前端框架；
- 多 worker 写 DuckDB；
- 第二套历史数据数据库。

历史数据能力继续运行在现有 FastAPI 单体进程中。

## 2.2 WriteCoordinator 约束

现有 `WriteCoordinator` 的核心约束必须严格遵守：

> 网络请求绝不能放在 WriteCoordinator 的写锁内。

因此历史同步必须采用：

```text
Tushare 请求
    ↓
解析 / 标准化 / 校验
    ↓
取得 WriteCoordinator
    ↓
开启数据库事务
    ↓
批量写入 + 更新水位
    ↓
Commit
    ↓
释放 WriteCoordinator
```

而不能：

```text
取得 WriteCoordinator
    ↓
请求 Tushare
    ↓
等待网络
    ↓
写库
```

首次历史回填可能持续较长时间，但只在每个交易日实际落库的短事务期间占用写锁。

这样现有：

- 行情刷新；
- 自选股操作；
- 用户操作；
- 估值刷新；

仍可以在历史数据的网络请求间隙正常获得写入机会。

---

# 3. 总体架构

新增的是 `History Data` **能力域**，不是另一套 Provider 框架。

MarketMind 现有 Provider 框架继续作为所有第三方信息获取的唯一入口：

```text
app/providers/base.py
    ├── 现有 Quote / Fundamental / Calendar Protocol
    └── 新增 HistoricalMarketDataProvider Protocol
                    │
                    ▼
app/providers/history/__init__.py
    HistoryProviderRegistry
    ├── 按 AppConfig 选择数据源
    ├── 复用 ProviderMetricsRegistry
    └── 复用 call_with_metrics(metrics；不设方法级 timeout)
                    │
                    ▼
app/providers/history/tushare.py
    TushareHistoricalMarketDataProvider
    ├── Tushare 字段 → MarketMind 内部标准模型
    └── 共享 TushareRequestGate / client factory
                    │
                    ▼
            HistorySyncService
        水位 / 重试 / 校验 / 编排
                    │
              网络调用在写锁外
                    │
                    ▼
        Repository + WriteCoordinator
                    │
                    ▼
                  DuckDB
```

交易日历不在 History Provider 中重新实现一遍。历史同步应扩展并复用现有：

```text
TushareTradingCalendarProvider
```

为它增加“严格历史模式/范围读取”能力：

```text
实时市场状态调用
    → 保持现有 fallback 行为

历史连续性同步
    → strict=True
    → Tushare 不可用即失败
    → 禁止 weekday fallback
```

这样 `trade_cal` 的上游访问、缓存表和市场日期语义只有一套实现。

---

# 3.1 现有 Provider 框架复用规则

当前项目已经具备以下 Provider 基础设施：

```text
app/providers/base.py
    内部标准数据模型 + Protocol

app/providers/quote/__init__.py
    QuoteProviderRegistry
    配置选源 / Provider 注册 / 分组分派 / metrics / timeout

app/providers/fundamental/tushare.py
    Tushare Fundamental Provider

app/providers/trading_calendar/provider.py
    Tushare Trading Calendar Provider + DuckDB 缓存

app/observability/provider_metrics.py
    ProviderMetricsRegistry
    call_with_metrics
    TimedFundamentalProvider

app/config.py
    providers.* + timeout.* 的统一配置模型
```

历史数据功能必须遵循相同模式。

### 必须复用

- `app/providers/base.py`：新增历史数据内部模型和 Protocol；
- `AppConfig`：历史 Provider 选择、timeout、history 配置全部由配置对象注入；
- Registry 模式：新增 `HistoryProviderRegistry`，职责与 `QuoteProviderRegistry` 一致；
- `ProviderMetricsRegistry`；
- `call_with_metrics`；
- 现有 Tushare timeout 配置；
- 现有 `TushareTradingCalendarProvider`；
- 现有 Provider 注入 Service/Job 的方式；
- 默认测试通过 fake/mock Provider 替换真实网络实现。

### 不允许重复建设

不得新增第二套：

```text
HistoryProviderMetricsRegistry
HistoryProviderTimeoutWrapper
HistoryProviderConfigLoader
独立 YAML 读取器
独立 trade_cal Provider
```

也不要创建：

```text
app/providers/history/base.py
```

作为第二个 Provider 基类文件。

历史 Provider Protocol 应进入现有：

```text
app/providers/base.py
```

### 为什么不能直接复用现有 `TushareFundamentalProvider`

`TushareFundamentalProvider` 的契约是：

- 输入当前 `Instrument` 列表；
- 只提取 PE(TTM) / PB / 股息率；
- 面向自选股 serving cache；
- 调用异常时降级为空结果。

历史 `daily_basic` 的契约则是：

- 按交易日获取全市场；
- 保存完整字段；
- 任何不可确认的失败都不能被吞掉；
- 失败直接影响该数据集的连续水位。

因此不应让历史任务调用现有 `TushareFundamentalProvider.get_fundamentals()`。

正确复用方式是：

```text
相同 Provider 框架
+
相同 Tushare transport / RequestGate / timeout / metrics
+
不同 capability Provider
```

而不是复用一个不匹配的业务接口。

---

# 4. 数据分类

## 4.1 证券与主档数据

保存：

- `stock_basic`
- `stock_company`
- `namechange`
- `trade_cal`

这些数据不使用“每日连续交易日水位”。

使用：

- 最近成功刷新时间；
- 当前状态；
- 可恢复游标（需要时）；
- 记录数；
- 最近错误。

## 4.2 日级事实数据

保存：

- `daily`
- `adj_factor`
- `daily_basic`
- `moneyflow`

这四个数据集分别维护独立连续水位线。

## 4.3 现有 `fundamental_snapshot` 的处理

**不把现有 `fundamental_snapshot` 改造成全市场 `daily_basic` 历史事实表。**

原因：

1. 当前表只保存自选股所需的少量估值字段；
2. 当前 `FundamentalRefreshJob` 服务于现有页面即时估值需求；
3. 全市场十多年 `daily_basic` 是大型分析事实表；
4. 两者生命周期和读取场景不同；
5. 直接替换会把历史数据改造与当前 UI 强耦合。

第一阶段采用：

```text
fundamental_snapshot
    = 当前页面使用的轻量 serving cache

market_daily_basic
    = Tushare daily_basic 全市场历史事实源
```

允许少量字段重叠。

未来若希望现有页面读取 `market_daily_basic`，应作为单独重构阶段处理。

---

# 5. 数据库设计原则

## 5.1 小表使用 ORM，超大事实表优先使用 SQLAlchemy Core

主档、状态表数据量较小，适合继续使用 ORM Model。

四张大型日级事实表预计达到千万级甚至更高记录量。

建议：

- 用 SQLAlchemy Core `Table` 定义事实表；
- Repository 使用 Core 批量写；
- 不对每条事实数据创建 Python ORM 实例；
- 不为大型事实表建立不必要的物理主键 / 外键约束；
- 通过应用层校验、批量替换与同步账本保证业务唯一性。

原因是 DuckDB 的主要价值在列式分析和批量扫描，而不是 OLTP 式逐行 ORM 操作。

## 5.2 日期类型

所有交易日期统一保存为：

```text
DATE
```

不把 `YYYYMMDD` 字符串原样作为事实表日期类型。

## 5.3 数值类型

行情与估值分析字段统一优先使用：

```text
DOUBLE
```

原因：

- 与 Tushare DataFrame 数值类型自然衔接；
- 避免 decimal 大量转换；
- 对行情分析精度足够；
- 与项目原历史数据规划一致。

明显属于整数状态的字段使用：

```text
INTEGER / SMALLINT
```

如：

- `limit_status`
- `employees`
- 状态计数。

资金流 `*_vol` 可使用 `BIGINT`；金额使用 `DOUBLE`。

## 5.4 时间戳

内部采集时间统一使用 timezone-aware UTC 时间戳。

业务交易日期仍按 `Asia/Shanghai` 理解。

---

# 6. 表结构设计总览

建议新增：

```text
cn_stock_basic
cn_stock_company
cn_stock_name_change

market_daily_bar
market_adj_factor
market_daily_basic
market_moneyflow

history_sync_state
history_day_status
history_sync_run
history_sync_run_dataset
```

并扩展现有：

```text
trading_calendar
```

---

# 7. `instrument` 与 A 股证券主档的关系

现有 `instrument` 继续作为 MarketMind 全资产统一主档：

```text
CN:STOCK:<symbol>
CN:ETF:<symbol>
HK:STOCK:<symbol>
...
```

Tushare 原始证券信息另存到：

```text
cn_stock_basic
```

二者关系为：

```text
instrument                    cn_stock_basic
─────────────────────         ─────────────────────
instrument_id     <───────>   instrument_id
symbol                        ts_code
name                          list_status
market                        list_date
asset_type                    delist_date
exchange                      ...
currency
is_active
```

## 7.1 Instrument 映射规则

从 `stock_basic` 同步时：

```text
instrument_id = "CN:STOCK:" + symbol
market        = "CN"
asset_type    = "STOCK"
name          = stock_basic.name
exchange      = stock_basic.exchange
currency      = "CNY"
is_active     = (list_status == "L")
```

关键要求：

> **不要再通过证券代码首位推断 SH/SZ/BJ。**

历史同步必须直接使用 Tushare 返回的 `ts_code` 和 `exchange`。

这样才能正确支持：

- SSE；
- SZSE；
- BSE；
- 已退市证券；
- 未来代码规则变化。

## 7.2 不删除历史 instrument

如果股票退市：

```text
is_active = false
list_status = "D"
```

但：

```text
instrument
cn_stock_basic
历史事实数据
```

都不能因为退市而删除。

---

# 8. `cn_stock_basic`

## 8.1 用途

保存 Tushare `stock_basic` 的完整证券主档。

## 8.2 建议结构

```text
cn_stock_basic
────────────────────────────────────
instrument_id        VARCHAR  PK / FK -> instrument
ts_code              VARCHAR  NOT NULL
symbol               VARCHAR  NOT NULL
name                 VARCHAR
area                 VARCHAR
industry             VARCHAR
fullname             VARCHAR
enname               VARCHAR
cnspell              VARCHAR
market               VARCHAR
exchange             VARCHAR
curr_type            VARCHAR
list_status          VARCHAR
list_date            DATE
delist_date          DATE
is_hs                VARCHAR
act_name             VARCHAR
act_ent_type         VARCHAR

source                VARCHAR NOT NULL
fetched_at            TIMESTAMPTZ NOT NULL
source_last_seen_at   TIMESTAMPTZ NOT NULL
sync_run_id           VARCHAR
```

小表可以建立主键 / 外键。

## 8.3 获取方式

为了避免默认只返回上市证券，也为了规避单次 6000 行风险，主动拆分：

```text
exchange:
    SSE
    SZSE
    BSE

list_status:
    L
    D
    P
    G
    UN
```

即按：

```text
exchange × list_status
```

逐个 shard 获取。

空 shard 允许。

如果任意非空 shard 恰好返回接口上限：

```text
row_count == 6000
```

视为潜在截断，本轮证券主档刷新失败，不提交为完整快照。

## 8.4 落库策略

所有 shard 先在内存中获取并校验。

全部成功后再一次性：

- upsert `instrument`；
- upsert `cn_stock_basic`；
- 更新主档同步状态。

不因为本轮接口结果里“暂时没出现”某个历史证券而物理删除旧证券。

---

# 9. `cn_stock_company`

## 9.1 建议结构

```text
cn_stock_company
────────────────────────────────────
instrument_id        VARCHAR PK / FK -> instrument
ts_code              VARCHAR NOT NULL
com_name             VARCHAR
com_id               VARCHAR
exchange             VARCHAR
chairman             VARCHAR
manager              VARCHAR
secretary            VARCHAR
reg_capital          DOUBLE
setup_date           DATE
province             VARCHAR
city                 VARCHAR
introduction         TEXT
website              VARCHAR
email                VARCHAR
office               TEXT
employees            INTEGER
main_business        TEXT
business_scope       TEXT

source               VARCHAR NOT NULL
fetched_at            TIMESTAMPTZ NOT NULL
sync_run_id           VARCHAR
```

## 9.2 获取方式

Tushare 当前接口单次上限 4500，因此不能做一次“全市场无条件请求”。

分别请求：

```text
SSE
SZSE
BSE
```

如果任何 exchange shard 命中最大返回量，不能把本轮刷新标记为完整。

## 9.3 刷新周期

默认：

```text
7 天刷新一次
```

管理员手动“检查并更新”也会在该数据过期时刷新。

不作为四个日级事实集推进的硬阻塞项。

---

# 10. `cn_stock_name_change`

## 10.1 建议结构

```text
cn_stock_name_change
────────────────────────────────────
event_key            VARCHAR PK
instrument_id        VARCHAR NOT NULL
ts_code              VARCHAR NOT NULL
name                 VARCHAR
start_date           DATE
end_date             DATE
ann_date             DATE
change_reason        VARCHAR

source               VARCHAR NOT NULL
fetched_at            TIMESTAMPTZ NOT NULL
sync_run_id           VARCHAR
```

`event_key` 为应用生成的稳定键。

建议：

```text
SHA-256(
    ts_code + "|" +
    name + "|" +
    start_date-or-empty
)
```

如果实际在线数据验证发现同一证券、同一名称、同一开始日存在多条不同事件，再将规范键扩展为：

```text
ts_code + name + start_date + end_date + ann_date
```

Claude Code 应通过在线 smoke test 验证，不应凭猜测修改产品语义。

## 10.2 首次初始化

为了保证历史名称完整，不依赖一次不确定总量的全市场请求。

建议首次 bootstrap：

```text
按 cn_stock_basic.ts_code 排序
        ↓
逐只调用 namechange(ts_code)
        ↓
成功后替换该证券全部 namechange 记录
        ↓
推进 master_cursor
```

`history_sync_state.master_cursor` 保存最后成功的 `ts_code`。

进程中断后从下一只继续。

## 10.3 后续增量

bootstrap 完成后默认每 7 天刷新近期变化。

使用：

```text
start_date = 上次成功日期 - 7 天
end_date   = 当前日期
```

保留 7 天重叠窗口，用于吸收上游迟到修订。

写入时：

- 删除重叠窗口内对应的旧事件；
- 插入当前完整返回集；
- 保留更早历史事件。

如果线上接口行为不适合全市场按日期增量，则 Provider 可以降级为“近期活跃证券逐只查询”，但不能改变历史事实连续水位语义。

---

# 11. 扩展 `trading_calendar`

现有 `trading_calendar` 继续使用，不新建重复交易日历表。

现有主键：

```text
(market, trade_date)
```

继续保留。

建议增加 nullable 字段：

```text
exchange       VARCHAR NULL
pretrade_date  DATE NULL
source         VARCHAR NULL
fetched_at     TIMESTAMPTZ NULL
```

设置 nullable 是为了兼容现有代码路径。

## 11.1 历史同步必须使用“严格交易日历”

现有市场状态功能允许某些场景在 Tushare 不可用时做近似工作日 fallback。

**历史连续性同步禁止使用近似日历。**

历史数据必须满足：

```text
market = "CN"
source = "tushare"
```

并且所需日期范围确实来自 Tushare `trade_cal`。

如果 Tushare Token 缺失或交易日历无法确认：

```text
历史同步失败 / 等待
```

不能用：

```text
周一～周五 ≈ 交易日
```

来推进历史水位。

这是为了避免因为春节、国庆、临时休市等情况制造错误的“缺数据日”。

## 11.2 Calendar 刷新策略

首次：

```text
2010-01-01 → 当前年度末
```

建议按年份获取，便于：

- 重试；
- 校验；
- 记录进度。

以后：

- 如果当前年度缺失或来源不是严格 Tushare，重新拉当前年度；
- 每年进入新年度时拉取新年度交易日历；
- 可以预先获取下一年度，但不是第一阶段强制要求。

交易日历是日级事实同步的硬前置条件。

---

# 12. 大型事实表设计

四张大型事实表不建议设置 ORM 物理主键 / 外键。

业务唯一键均为：

```text
(instrument_id, trade_date)
```

唯一性由：

- 每日完整替换；
- 写入前重复检查；
- `history_day_status`；
- 集成测试；

共同保证。

不在第一阶段对大型事实表建立额外二级索引。

原因：

- 当前第一目标是全量写入与按日期同步；
- 数据天然按照 trade_date 顺序追加；
- DuckDB 列式扫描可以高效处理分析查询；
- 未来真正上线“单股多年 K 线”等读取场景后，再依据真实 benchmark 决定是否增加索引/排序副本/物化层。

---

# 13. `market_daily_bar`

```text
market_daily_bar
────────────────────────────────────
instrument_id    VARCHAR NOT NULL
ts_code          VARCHAR NOT NULL
trade_date       DATE NOT NULL

open             DOUBLE
high             DOUBLE
low              DOUBLE
close            DOUBLE
pre_close        DOUBLE
change           DOUBLE
pct_chg          DOUBLE
vol              DOUBLE
amount           DOUBLE
ah_vol           DOUBLE NULL
ah_amount        DOUBLE NULL

source           VARCHAR NOT NULL
fetched_at       TIMESTAMPTZ NOT NULL
```

说明：

- 原始未复权行情；
- `ah_vol` / `ah_amount` 历史为空属于合法数据；
- 不存 qfq / hfq；
- 同步执行归属通过：

```text
dataset = daily
trade_date
    ↓
history_day_status.completed_by_run_id
```

查询，避免为几千万事实行重复保存长 UUID。

---

# 14. `market_adj_factor`

```text
market_adj_factor
────────────────────────────────────
instrument_id    VARCHAR NOT NULL
ts_code          VARCHAR NOT NULL
trade_date       DATE NOT NULL
adj_factor       DOUBLE NOT NULL

source           VARCHAR NOT NULL
fetched_at       TIMESTAMPTZ NOT NULL
```

`adj_factor` 必须：

```text
> 0
```

长期事实为：

```text
raw daily + adj_factor
```

复权价格在查询/分析层计算。

---

# 15. `market_daily_basic`

```text
market_daily_basic
────────────────────────────────────
instrument_id       VARCHAR NOT NULL
ts_code             VARCHAR NOT NULL
trade_date          DATE NOT NULL

close               DOUBLE
turnover_rate       DOUBLE
turnover_rate_f     DOUBLE
volume_ratio        DOUBLE
pe                  DOUBLE
pe_ttm              DOUBLE
pb                  DOUBLE
ps                  DOUBLE
ps_ttm              DOUBLE
dv_ratio            DOUBLE
dv_ttm              DOUBLE
total_share         DOUBLE
float_share         DOUBLE
free_share          DOUBLE
total_mv            DOUBLE
circ_mv             DOUBLE
limit_status        SMALLINT

source              VARCHAR NOT NULL
fetched_at           TIMESTAMPTZ NOT NULL
```

NULL 必须保留原义。

例如：

- 亏损导致 PE 为 NULL；
- 尚无股息率；
- Tushare 某字段历史阶段缺失。

不能为了通过校验把 NULL 强制转 0。

---

# 16. `market_moneyflow`

```text
market_moneyflow
────────────────────────────────────
instrument_id       VARCHAR NOT NULL
ts_code             VARCHAR NOT NULL
trade_date          DATE NOT NULL

buy_sm_vol          BIGINT
buy_sm_amount       DOUBLE
sell_sm_vol         BIGINT
sell_sm_amount      DOUBLE

buy_md_vol          BIGINT
buy_md_amount       DOUBLE
sell_md_vol         BIGINT
sell_md_amount      DOUBLE

buy_lg_vol          BIGINT
buy_lg_amount       DOUBLE
sell_lg_vol         BIGINT
sell_lg_amount      DOUBLE

buy_elg_vol         BIGINT
buy_elg_amount      DOUBLE
sell_elg_vol        BIGINT
sell_elg_amount     DOUBLE

net_mf_vol          BIGINT
net_mf_amount       DOUBLE

source              VARCHAR NOT NULL
fetched_at           TIMESTAMPTZ NOT NULL
```

注意：

- 主动买/卖数量与金额应为非负或 NULL；
- `net_mf_vol`、`net_mf_amount` 可为正、负或 0；
- 不根据证券主档数量推断 `moneyflow` 应有多少行；
- Tushare 实际接口覆盖范围本身可能小于 A 股证券主档范围。

---

# 17. 同步状态模型

## 17.1 `history_sync_state`

每个数据集一行。

建议同时管理：

- 四个日级数据集；
- 四个主档数据集。

```text
history_sync_state
────────────────────────────────────────────
dataset                     VARCHAR PK
dataset_kind                VARCHAR NOT NULL

status                      VARCHAR NOT NULL

history_start_date          DATE NULL

latest_complete_trade_date  DATE NULL
latest_expected_trade_date  DATE NULL
current_trade_date          DATE NULL
current_attempt             INTEGER NOT NULL DEFAULT 0

master_cursor               VARCHAR NULL
bootstrap_complete          BOOLEAN NOT NULL DEFAULT false

record_count                BIGINT NOT NULL DEFAULT 0
data_min_date               DATE NULL
data_max_date               DATE NULL

last_started_at             TIMESTAMPTZ NULL
last_success_at             TIMESTAMPTZ NULL
last_error_at               TIMESTAMPTZ NULL
last_error_code             VARCHAR NULL
last_error                  TEXT NULL

updated_at                  TIMESTAMPTZ NOT NULL
```

### `dataset_kind`

固定：

```text
DAILY_CONTIGUOUS
MASTER
```

### Dataset 名称

第一阶段固定：

```text
stock_basic
trade_cal
namechange
stock_company

daily
adj_factor
daily_basic
moneyflow
```

不要把名称散落成任意字符串。

代码层定义 Enum / Literal 常量。

---

# 18. `history_day_status`

这是四个日级数据集的“连续完成证明”。

```text
history_day_status
────────────────────────────────────────
dataset                 VARCHAR NOT NULL
trade_date              DATE NOT NULL
status                  VARCHAR NOT NULL
row_count               BIGINT NOT NULL
fetched_at              TIMESTAMPTZ NOT NULL
completed_at            TIMESTAMPTZ NOT NULL
completed_by_run_id     VARCHAR NOT NULL

PRIMARY KEY(dataset, trade_date)
```

第一阶段持久化的日状态主要是：

```text
COMPLETE
```

失败详情放到：

- `history_sync_state`
- `history_sync_run_dataset`

而不是为每次失败不断在 `history_day_status` 增长记录。

## 18.1 为什么既要 state 又要 day status

`history_sync_state.latest_complete_trade_date`：

- 快速告诉系统下一次从哪里开始；
- O(1) 获取。

`history_day_status`：

- 给连续水位一个可验证的账本；
- 支持崩溃恢复；
- 支持管理员追查；
- 支持检测 state 异常；
- 只有约：

```text
4 个数据集 × 约 4000 个交易日
```

量级非常小。

---

# 19. `history_sync_run`

每次统一同步任务一条记录。

```text
history_sync_run
────────────────────────────────────────
run_id                VARCHAR PK
trigger_type          VARCHAR NOT NULL
requested_by_user_id  VARCHAR NULL

status                VARCHAR NOT NULL

started_at            TIMESTAMPTZ NOT NULL
finished_at           TIMESTAMPTZ NULL

error_summary         TEXT NULL
created_at            TIMESTAMPTZ NOT NULL
```

### `trigger_type`

```text
SCHEDULED
MANUAL
STARTUP
```

### `status`

```text
RUNNING
SUCCESS
PARTIAL
FAILED
INTERRUPTED
NOOP
```

含义：

- `SUCCESS`：需要处理的数据集全部追到自己的当前目标；
- `PARTIAL`：至少一个数据集成功推进，但至少一个未追平；
- `FAILED`：任务无法进行或没有任何需要推进的数据集成功完成；
- `NOOP`：所有数据已经追平，不需要写数据；
- `INTERRUPTED`：上次进程终止时仍为 RUNNING，启动恢复时标记。

---

# 20. `history_sync_run_dataset`

每个 run × dataset 一行。

```text
history_sync_run_dataset
────────────────────────────────────────
run_id                    VARCHAR NOT NULL
dataset                   VARCHAR NOT NULL

status                    VARCHAR NOT NULL

start_watermark           DATE NULL
target_trade_date         DATE NULL
end_watermark             DATE NULL

start_cursor              VARCHAR NULL
end_cursor                VARCHAR NULL

dates_completed           INTEGER NOT NULL DEFAULT 0
rows_written              BIGINT NOT NULL DEFAULT 0
request_count             INTEGER NOT NULL DEFAULT 0
retry_count               INTEGER NOT NULL DEFAULT 0

failed_trade_date         DATE NULL
last_error_code           VARCHAR NULL
last_error                TEXT NULL

started_at                TIMESTAMPTZ NOT NULL
finished_at               TIMESTAMPTZ NULL

PRIMARY KEY(run_id, dataset)
```

管理员页面的“最近执行记录”主要读取这两张 run 表。

---

# 21. 日级水位线算法

这是技术实现的核心。

## 21.1 初始状态

第一次初始化：

```text
history_start_date = 2010-01-01
latest_complete_trade_date = NULL
```

不人为创建：

```text
2009-12-31
```

这种虚假水位。

Planner 根据严格交易日历得到：

```text
第一个 >= 2010-01-01 的 open day
```

作为第一天。

## 21.2 后续状态

如果：

```text
latest_complete_trade_date = 2025-09-01
target_trade_date          = 2026-09-16
```

则：

```text
SELECT trade_date
FROM trading_calendar
WHERE market = 'CN'
  AND source = 'tushare'
  AND is_open = true
  AND trade_date > '2025-09-01'
  AND trade_date <= '2026-09-16'
ORDER BY trade_date
```

得到完整待处理列表。

## 21.3 严格顺序

必须：

```text
for trade_date in pending_dates ordered ASC:
    process(trade_date)

    if success:
        advance watermark
    else:
        stop this dataset
```

禁止：

```text
失败
↓
continue
↓
同步后续日期
```

---

# 22. 每日原子提交事务

对某数据集某交易日，事务边界固定为“一个数据集的一天”。

流程：

```text
1. 请求 Tushare                 ← 无写锁
2. Normalize                   ← 无写锁
3. Validate                    ← 无写锁
4. 进入 WriteCoordinator
5. BEGIN
6. 查询该日期已有行数 old_count
7. DELETE 当天该数据集旧行
8. 批量 INSERT 当前完整数据
9. UPSERT history_day_status=COMPLETE
10. UPDATE history_sync_state:
      latest_complete_trade_date = 当前日期
      record_count += new_count - old_count
      data_min_date / data_max_date
      status / progress
11. UPDATE history_sync_run_dataset
12. COMMIT
13. 退出 WriteCoordinator
```

步骤 7～11 必须处于同一个数据库事务。

---

# 23. 为什么使用“整日替换”而不是逐行 UPSERT

对于一个已经成功获取完整快照的交易日：

```text
DELETE WHERE trade_date = ?
INSERT full_day_rows
```

比逐行 UPSERT 更适合本场景。

原因：

1. 单日最多约几千行；
2. 重跑某一天时，可以吸收 Tushare 历史修订；
3. 如果上游删除/更正某条记录，逐行 UPSERT 会留下旧行；
4. 整日替换逻辑更容易证明幂等；
5. 与“该交易日整体成功后才推进水位”的产品语义一致。

---

# 24. 原子性保证

如果在：

```text
DELETE
```

以后、`INSERT` 中途异常：

```text
ROLLBACK
```

旧数据仍然存在，水位不动。

如果事实表写完但：

```text
history_sync_state update
```

失败：

```text
ROLLBACK
```

事实数据同样不提交。

因此数据库不会出现：

```text
水位已经到 D
但 D 的事实数据只写了一半
```

也不会出现：

```text
旧数据被删
新数据失败
结果 D 整天为空
```

---

# 25. 水位一致性检查

每次统一任务开始时，对四个日级数据集执行轻量 reconcile。

至少检查：

1. `latest_complete_trade_date` 是否存在；
2. 如果存在，`history_day_status` 在该日期是否为 `COMPLETE`；
3. 从 `history_start_date` 到 watermark 的交易日是否均有 COMPLETE 账本；
4. 事实表 `MAX(trade_date)` 是否与 state 存在明显矛盾。

关键原则：

> `MAX(trade_date)` 只能用来发现矛盾，不能用来自动把水位向前推进。

## 25.1 如果发现 state/ledger 不一致

采取保守恢复：

```text
从 history_start_date 开始
按严格交易日历顺序扫描 history_day_status
找到第一个缺失 COMPLETE 的交易日
```

将可信连续水位恢复到它的前一个交易日。

然后从缺失日重新同步。

绝不能根据事实表中某个更晚日期自动跳过中间缺口。

---

# 26. latest_expected_trade_date

不能简单使用：

```text
today()
```

每个数据集都必须通过：

```text
AvailabilityPolicy
```

计算自己的当前目标。

输入：

- 当前 `Asia/Shanghai` 时间；
- 严格交易日历；
- 数据集发布时间规则。

输出：

```text
latest_expected_trade_date
```

---

# 27. 各数据集默认可用时间

建议默认配置/常量：

| 数据集 | Tushare 当前说明 | MarketMind 默认可用时间 |
|---|---|---|
| `adj_factor` | 盘前约 09:15～09:20 | 09:30 |
| `daily` | 收盘后约 15～16 点 | 16:30 |
| `daily_basic` | 收盘后约 15～17 点 | 17:30 |
| `moneyflow` | 文档未给出足够稳定的精确时间 | 20:30（保守默认） |

`moneyflow` 的 20:30 属于 MarketMind 的保守运行策略，不代表 Tushare 官方承诺。

必须可通过后续代码配置调整，而不能散落为多个 magic number。

## 27.1 盘中管理员手动触发

例如交易日上午 10:00：

```text
daily target = 上一个已收盘且理论可用的交易日
```

不能把“今天还没收盘”当作同步失败。

## 27.2 周末触发

例如周六：

```text
target = 最近一个已达到发布时间要求的 open day
```

---

# 28. 统一调度时间

统一 History Sync Job 默认每天：

```text
20:30 Asia/Shanghai
```

执行。

建议每天都运行，包括周六、周日。

原因：

- 周末运行不会产生虚假交易日；
- 如果周五任务因停机错过，周六仍能追平；
- 水位规划本身会自动判断是否有 pending dates。

同时增加：

```text
startup_catchup = true
```

应用启动后，如果发现任意数据集落后当前目标，自动触发一次 `STARTUP` 同步。

这样服务器错过定时点后不必等到第二天 20:30。

---

# 29. Retry 设计

产品要求：

```text
同一 dataset + trade_date
单次 run 最多尝试 10 次
```

## 29.1 默认退避

建议：

```python
delay = min(5 * 2 ** (attempt - 1), 300)
delay = delay * random(0.8, 1.2)
```

即大致：

```text
5s
10s
20s
40s
80s
160s
300s
300s
300s
```

10 次尝试之间最多有 9 次等待。

所有数字放入配置。

## 29.2 可重试错误

包括：

- 网络超时；
- Tushare 临时服务错误；
- 限流；
- 短暂空结果；
- 解析失败；
- 数据质量暂时不满足；
- 潜在截断且 fallback 仍失败。

## 29.3 不应盲目重试的配置错误

例如：

- 未配置 Tushare Token；
- Token 明确无权限；
- 代码字段映射错误；
- 数据库 schema 不匹配。

这类错误应尽快失败并显示明确原因，而不是无意义睡眠 10 轮。

## 29.4 10 次失败后

该数据集：

```text
status = FAILED
failed_trade_date = 当前日期
watermark 不变
```

本轮停止继续处理该数据集。

统一任务继续尝试其他独立数据集。

下一次定时/手动运行仍从该失败日期重新开始。

---

# 30. Tushare 全局请求协调

历史回填期间会大量调用 Tushare。

现有：

- `TushareFundamentalProvider`；
- `TushareTradingCalendarProvider`；
- 新 `TushareHistoricalMarketDataProvider`；

共享同一个 Token。

因此新增：

```text
app/providers/tushare_common.py
```

它是 **Provider 内部的 transport helper**，不是新的 Provider 框架。

建议包含：

```text
TushareRequestGate
create_tushare_pro_client(config)
```

## 30.1 与现有 ProviderMetrics 的关系

调用层级固定为：

```text
HistoryProviderRegistry
        ↓
call_with_metrics(...)
        ↓
TushareHistoricalMarketDataProvider.method(...)
        ↓
TushareRequestGate
        ↓
Tushare SDK
```

也就是说：

- `call_with_metrics` 负责方法级成功/错误/超时**计数**与整方法耗时；
- `TushareRequestGate` 只负责进程级请求节奏；
- 两者不能互相替代。

不要新增第二套 metrics/timeout 代码。

**超时归属（v0.3.0 实测修正）**：`call_with_metrics` 对历史 Provider 方法
**不设方法级 wall-clock timeout**。一个方法可能包含多个受 gate 限流的真实
请求（`stock_basic` 15 个分片、按证券逐只补齐上千次），固定上限会把正常
节流误判为超时（15 分片 × 1.25s ≈ 17.5s > 15s），且超时后会留下仍在发
请求的线程与重试重叠。

- 单请求网络超时放在**共享 Tushare transport / 原生 SDK 请求层**：
  `ts.pro_api(token, timeout=config.providers.timeout.tushare)`，SDK 在
  `DataApi.query` 内以 `requests.post(..., timeout=T)` 对每次调用只发的那
  一个 HTTP 请求生效；client 构造本身不发请求；
- 等待 gate 的时间**不计入**单请求网络超时（先 `gate.acquire()` 再发请求）；
- 真实请求超时归一化为 `TushareTimeoutError`（同时是 `TimeoutError` 子类），
  因而仍然进入 `timeout_count`，不会退化成普通 `error_count`；
- 方法级只记录 success/error/duration，超时分类看异常类型而非是否传了
  `timeout`。

## 30.2 RequestGate 作用

所有 Tushare 请求进入同一个线程安全 gate：

```text
threading.Lock
+
time.monotonic()
+
endpoint min interval
```

避免：

```text
history job
+
fundamental job
+
calendar provider
```

同时撞 Tushare 限流。

## 30.3 默认请求节奏

历史普通请求建议保守默认：

```text
min_interval_seconds = 0.6
```

约 100 次/分钟。

`stock_basic` 文档当前限额更严格，建议：

```text
>= 1.25 秒 / 请求
```

具体由 endpoint policy 管理。

第一阶段不做高并发 Tushare 请求。

## 30.4 渐进接入现有 Tushare Provider

Claude Code 应让：

- `TushareFundamentalProvider`；
- `TushareTradingCalendarProvider`；
- `TushareHistoricalMarketDataProvider`；

共享 `create_tushare_pro_client()` / `TushareRequestGate`。

改造过程中不得改变旧 Provider 的业务契约和现有页面行为。

---

# 31. 历史 Provider 接口与 Registry

## 31.1 Protocol 放在现有 `app/providers/base.py`

不要新增平行的 `providers/history/base.py`。

在现有 `app/providers/base.py` 中增加历史数据内部标准模型，例如：

```text
StockBasicRecord
StockCompanyRecord
StockNameChangeRecord

DailyBar
AdjFactor
DailyBasic
MoneyFlow

ProviderBatch[T]
```

以及：

```python
@runtime_checkable
class HistoricalMarketDataProvider(Protocol):
    def get_stock_basic(self) -> ProviderBatch[StockBasicRecord]: ...
    def get_stock_company(self) -> ProviderBatch[StockCompanyRecord]: ...
    def get_name_changes(...) -> ProviderBatch[StockNameChangeRecord]: ...

    def get_daily(self, trade_date: date, instruments: list[Instrument]) -> ProviderBatch[DailyBar]: ...
    def get_adj_factors(self, trade_date: date, instruments: list[Instrument]) -> ProviderBatch[AdjFactor]: ...
    def get_daily_basic(self, trade_date: date, instruments: list[Instrument]) -> ProviderBatch[DailyBasic]: ...
    def get_moneyflow(self, trade_date: date, instruments: list[Instrument]) -> ProviderBatch[MoneyFlow]: ...
```

精确方法签名可依据实现细节调整，但必须遵守：

> 第三方原始字段名和 Tushare DataFrame 不越过具体 Provider 边界。

这与现有 `app/providers/base.py` 的设计原则一致。

## 31.2 `ProviderBatch`

历史 Provider 需要比实时 Quote Provider 多返回一点采集元信息。

建议：

```python
@dataclass(frozen=True)
class ProviderBatch(Generic[T]):
    records: list[T]
    source: str
    raw_row_count: int
    truncation_risk: bool = False
```

如截断 fallback 需要更多信息，可增加通用元字段，但不能塞 Tushare SDK 对象或原始 DataFrame。

## 31.3 Concrete Provider

新增：

```text
app/providers/history/tushare.py
```

实现：

```text
TushareHistoricalMarketDataProvider
```

职责：

- 调 Tushare；
- 显式 `fields`；
- Tushare schema 基础校验；
- Tushare 日期/代码解析；
- Tushare `ts_code` → MarketMind `instrument_id` 映射；
- Tushare 字段 → 内部标准模型；
- 判断当前请求是否触达已知 API 行数上限；
- 返回 `ProviderBatch`。

它不负责：

- 水位推进；
- 10 次重试编排；
- DB transaction；
- 决定下一交易日；
- 吞掉异常并返回空集合。

历史连续性依赖异常能够向 Service 传播。

## 31.4 HistoryProviderRegistry

新增：

```text
app/providers/history/__init__.py
```

其模式应直接参照现有：

```text
QuoteProviderRegistry
```

建议：

```python
_PROVIDERS = {
    "tushare": TushareHistoricalMarketDataProvider,
}

class HistoryProviderRegistry:
    ...
```

职责：

- 从 `config.providers.history` 选择 Provider；
- 构造单例 Provider；
- 调用 `call_with_metrics`（只记录方法级 success/error/duration，不设方法级
  wall-clock 超时；单请求超时由 Provider 构造的 transport 注入）；
- 为 Service 提供稳定的历史数据获取入口。

历史 sync service 不直接：

```python
TushareHistoricalMarketDataProvider(...)
```

也不直接 import `tushare`。

## 31.5 Metrics key

继续复用 `ProviderMetricsRegistry`。

为便于管理员排查历史 endpoint，可使用：

```text
tushare_history_stock_basic
tushare_history_daily
tushare_history_adj_factor
tushare_history_daily_basic
tushare_history_moneyflow
```

作为 metrics source key。

这是现有 Registry 的不同 key，不是新的 metrics 系统。

## 31.6 交易日历是例外：直接复用现有 Provider

不要在 `HistoricalMarketDataProvider` 中再实现 `trade_cal`。

扩展：

```text
TushareTradingCalendarProvider
```

新增类似：

```python
get_days(
    market: str,
    start_date: date,
    end_date: date,
    *,
    strict: bool = False,
) -> list[TradingCalendarDay]
```

或等价能力。

`strict=False`：

- 保持现有实时市场状态的 fallback 行为。

`strict=True`：

- 必须来自 Tushare；
- 不允许 weekday fallback；
- 不允许用 `calibrate.get(day, weekday)` 补造缺失日；
- 上游数据不完整时抛异常；
- 返回/缓存 `pretrade_date` 和 source 元信息。

HistorySyncService 只使用 strict 模式。

---

# 32. 所有字段必须显式声明

字段常量位于具体 Tushare Provider 内部，例如：

```text
TUSHARE_DAILY_FIELDS
TUSHARE_DAILY_BASIC_FIELDS
```

而不是放到通用 Protocol 中。

对于支持 `fields` 的接口，不依赖 Tushare 默认返回列。

代码中为每个 dataset 定义：

```text
EXPECTED_FIELDS
REQUIRED_FIELDS
OPTIONAL_FIELDS
```

例如：

```text
DAILY_FIELDS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
    "ah_vol",
    "ah_amount",
]
```

如果将来 Tushare新增字段：

- Provider 不会因默认返回变化突然改变 DB 写入；
- 新字段需要经过显式 migration + schema 更新；
- 旧程序仍稳定。

---

# 33. 6000 行截断防护

这是数据完整性最重要的异常场景之一。

## 33.1 日级接口

对于当前存在 6000 行上限的：

- `daily`
- `daily_basic`
- `moneyflow`

如果：

```text
row_count < 6000
```

继续正常校验。

如果：

```text
row_count == 6000
```

必须认为：

```text
TRUNCATION_RISK
```

不能直接推进水位。

## 33.2 Fallback 策略

遇到截断风险：

1. 从 `cn_stock_basic` 获取该交易日可能存在的证券代码集合；
2. 采用更细粒度请求；
3. 合并结果；
4. 去重；
5. 再执行完整校验；
6. 只有能够确认不再被上限截断时才允许提交。

对于文档明确支持多证券代码的接口，可以批量分组。

对于文档没有明确保证多代码参数的接口：

> 不猜测参数能力。

使用安全的逐证券方式，或通过在线 smoke test 确认后再增加 batch 优化。

**在线实测结论（v0.3.0，真实 Token）**：

| 接口 | 逗号分隔多 `ts_code` | 结论 |
| --- | --- | --- |
| `daily` / `moneyflow` / `adj_factor` | 支持（1000 代码全部返回） | 可用批量分组 |
| `daily_basic` | **静默返回 0 行**（不报错） | **只能逐只** |

`daily_basic` 的多代码"静默空"尤其危险：它把"没查到"伪装成"查过了"，
一旦复用其他接口的 multi-code fallback，缺失交易日会被判成正常完成、水位
照常推进且无任何错误码暴露。因此 `daily_basic` 的截断补齐按
**候选集 − 已返回代码**逐只查询：

1. 正常路径仍按 `trade_date` 取全市场；
2. 仅当返回行数**恰好等于 6000**（命中上限）时触发补齐；
3. 候选集由 `cn_stock_basic` 的 `list_date`/`delist_date` 生成——即该交易日
   可能产生行情的证券；
4. 只对"候选集 − 已返回代码"的缺失证券逐只查询并合并；
5. 每个缺失证券必须得到"有记录"或**明确空结果**（停牌等自然缺失，允许为
   空）；任一请求**异常**则该交易日不 COMPLETE、水位不推进，走正常重试；
6. 不依赖官方文档未声明的 `offset`/`limit` 分页。

`daily` 的行数（5553）与 `daily_basic`（5553）一致，而 `adj_factor`（5565）
多出 12 只——停牌证券仍有复权因子但当日无行情。候选集构造必须容纳这种
"同一天不同数据集覆盖不同"的现实，不能拿一个数据集的返回行数去校验另一个。

## 33.3 `stock_basic`

主动按：

```text
exchange × list_status
```

拆分，避免单次超过 6000。

## 33.4 `stock_company`

按 exchange 拆分，避免 4500 上限。

## 33.5 `adj_factor`

当前文档没有与前三者完全相同的 6000 行描述，但实现仍应对：

```text
异常固定阈值
明显不合理的返回量
```

保持保守。

如果在线验证发现同样存在可触达的最大返回限制，则加入同类 fallback。

---

# 34. 空结果处理

对一个严格交易日的日级数据集：

```text
0 rows
```

不能自动视为成功。

## 34.1 历史日期空结果

如果日期明显早于当前：

```text
ERROR_EMPTY_RESULT
```

进入重试。

10 次仍空：

```text
FAILED
watermark 不推进
```

## 34.2 当前最新交易日空结果

如果接近发布时间：

```text
WAITING_SOURCE
```

不把它当成已完整。

可以在本轮按重试策略再次请求。

若仍未生成：

- 本次该数据集不推进；
- 页面显示“等待数据源”；
- 下一次任务继续。

## 34.3 证券级缺失不是日期级空结果

允许：

- 停牌证券没有 `daily`；
- moneyflow 对某些市场不覆盖；
- 某些指标字段为 NULL。

不能把“某证券没行”直接等同于“整天不完整”。

---

# 35. 通用数据校验

校验分两层。

### Provider 边界校验

具体 Tushare Provider 负责：

1. Tushare DataFrame / 返回结构可解析；
2. 请求声明的 Tushare 必要字段存在；
3. `ts_code` / `trade_date` 等 Tushare 原始键可解析；
4. 原始数值可以转换；
5. Tushare 行数上限风险可以识别；
6. 成功转换为 MarketMind 内部标准模型。

Tushare DataFrame 到此为止，不进入 Service/Repository。

### Domain 校验

`HistoryValidationService` 对 `ProviderBatch` 检查：

1. `instrument_id` 非空；
2. `trade_date` 非空；
3. 所有记录日期与请求日期一致；
4. `(instrument_id, trade_date)` 不重复；
5. 行数不为 0（除非接口语义明确允许且经过专门规则）；
6. `truncation_risk == false`；
7. 不存在 NaN/Inf 等不能安全落库的异常数值；
8. instrument 能映射至证券主档；
9. 通过各数据集专项业务规则。

## 35.1 未知 `ts_code`

如果事实数据出现主档中没有的 `ts_code`：

```text
刷新一次 stock_basic
        ↓
重新映射
```

如果仍然未知：

```text
VALIDATION_ERROR_UNKNOWN_INSTRUMENT
```

该交易日不推进水位。

禁止临时创建：

```text
CN:STOCK:UNKNOWN
```

之类占位证券。

---

# 36. `daily` 专项校验

建议检查：

```text
open/high/low/close >= 0
vol >= 0
amount >= 0
```

当 OHLC 均有值且 > 0 时：

```text
high >= open
high >= close
high >= low

low <= open
low <= close
```

但不通过“当日上市证券数量”校验完整性。

允许：

```text
ah_vol = NULL
ah_amount = NULL
```

尤其是历史时期。

如果线上真实数据存在合法特殊值，应根据 smoke test 调整 Validator，而不是在 Provider 层偷偷修值。

---

# 37. `adj_factor` 专项校验

每条：

```text
adj_factor > 0
```

重复 `ts_code` 不允许。

---

# 38. `daily_basic` 专项校验

允许估值字段 NULL。

建议检查非 NULL 时：

```text
total_share >= 0
float_share >= 0
free_share >= 0
total_mv >= 0
circ_mv >= 0
```

`limit_status`：

```text
NULL 或 Tushare 当前文档允许的枚举范围
```

不要求：

```text
pe / pe_ttm 非空
```

否则会把亏损股误判为数据缺失。

---

# 39. `moneyflow` 专项校验

所有：

```text
buy_*_vol
sell_*_vol
buy_*_amount
sell_*_amount
```

非 NULL 时应：

```text
>= 0
```

允许：

```text
net_mf_vol
net_mf_amount
```

为负数。

不自行计算替代官方 `net_mf_*`。

---

# 40. 日级数据同步编排

统一 Service：

```text
HistorySyncService
```

建议职责：

```text
run(trigger, requested_by)
    ├── recover_stale_runs()
    ├── ensure_master_prerequisites()
    ├── reconcile_daily_watermarks()
    ├── sync_due_master_datasets()
    ├── sync_daily_dataset("daily")
    ├── sync_daily_dataset("adj_factor")
    ├── sync_daily_dataset("daily_basic")
    ├── sync_daily_dataset("moneyflow")
    └── finalize_run()
```

## 40.1 顺序建议

先：

```text
trade_cal
stock_basic
```

因为它们是硬前置。

然后可刷新：

```text
stock_company
namechange
```

它们失败不阻塞日级事实数据。

最后四个日级数据集。

## 40.2 日级数据集之间不互相阻塞

例如：

```text
daily       → 成功
adj_factor  → 成功
daily_basic → 失败
moneyflow   → 仍继续尝试
```

最后：

```text
run.status = PARTIAL
```

---

# 41. Master 数据刷新频率

建议：

| 数据集 | 默认策略 |
|---|---|
| `trade_cal` | 缺失即刷新；当前年度定期确认 |
| `stock_basic` | 每日统一任务前，如果超过 24 小时未成功则刷新 |
| `stock_company` | 每 7 天 |
| `namechange` | bootstrap 完成后每 7 天增量 |

这四个间隔可在配置中集中管理。

---

# 42. 首次全量回填

首次运行：

```text
初始化 sync_state
    ↓
严格 trade_cal 2010→当前
    ↓
stock_basic 全状态
    ↓
必要 master refresh
    ↓
四个 daily dataset 独立执行
```

例如：

```text
daily:
    2010-01-04
    2010-01-05
    ...
    target

adj_factor:
    2010-01-04
    2010-01-05
    ...
    target
```

不人为限制：

```text
只回填最近 30 天
```

如果因限流/网络在某天失败 10 次：

```text
该 dataset 停止
```

下一次从原失败日期继续。

---

# 43. Dataset 处理伪代码

```python
def sync_daily_dataset(dataset, target_date, run_id):
    state = load_state(dataset)

    reconcile_if_needed(dataset, state)

    pending_dates = calendar.open_days_after(
        state.latest_complete_trade_date,
        start_date=state.history_start_date,
        end_date=target_date,
    )

    for trade_date in pending_dates:
        set_progress(dataset, trade_date)

        try:
            frame = retry_up_to_10(
                lambda: fetch_validate_with_fallback(dataset, trade_date)
            )
        except Exhausted:
            mark_dataset_failed(dataset, trade_date)
            return FAILED

        commit_one_day_atomically(
            dataset=dataset,
            trade_date=trade_date,
            frame=frame,
            run_id=run_id,
        )

    mark_dataset_caught_up(dataset)
    return SUCCESS
```

`retry_up_to_10()` 仅包围：

- 请求；
- normalize；
- validate；
- 截断 fallback。

数据库事务失败也可以进入有限重试，但必须注意：

- WriteCoordinator 自己已有 DB conflict retry；
- 不要把同一数据库冲突重试嵌套成无界次数；
- 最终仍以单日期最多 10 次“同步尝试”的产品语义作为上层边界。

---

# 44. 批量写入策略

单个交易日一般是几千行。

第一阶段优先使用：

```text
SQLAlchemy Core executemany
```

而不是逐行 ORM。

推荐：

```python
session.execute(fact_table.insert(), records)
```

如实际驱动在单批数千行上存在问题，可按：

```text
1000～2000 行
```

分 chunk，但仍保持在同一个交易日事务中。

## 44.1 性能原则

禁止：

```python
for row in rows:
    session.add(Model(...))
```

用于大事实表。

## 44.2 后续可优化点

如果 benchmark 证明 Core executemany 无法满足首次回填性能，可只替换 Repository 内部实现为 DuckDB staging / DataFrame register + `INSERT SELECT`。

Service、Validator、水位事务语义不变。

也就是说：

> 性能优化只能替换“怎么批量写”，不能破坏“整天原子提交”。

---

# 45. 记录数与管理员统计

管理员页面不能每次刷新都：

```sql
SELECT COUNT(*) FROM market_daily_bar;
```

扫描千万级事实表。

`history_sync_state` 保存：

```text
record_count
data_min_date
data_max_date
```

整日替换时：

```text
old_count = DELETE 前当天已有行数
new_count = 当前合法行数

record_count += new_count - old_count
```

与事实写入处于同一个事务。

这样管理员 Summary 查询只读小表。

---

# 46. 数据重跑与幂等

必须覆盖以下场景：

### 场景 A：请求超时，但实际上上游已返回

下一次重复请求，不影响 DB。

### 场景 B：进程在写库前退出

水位没动，下次继续同一天。

### 场景 C：进程在事务中退出

DuckDB 回滚未完成事务，水位没动。

### 场景 D：事务成功，但进程尚未刷新 UI 就退出

事实 + ledger + watermark 已一起 Commit。

下一次看到水位已经推进，不重复处理。

### 场景 E：人为/恢复逻辑重新处理已存在日期

整日 DELETE + INSERT，不产生重复行。

---

# 47. 任务互斥

统一历史同步只能有一个实例运行。

建议：

```text
HistorySyncJob
```

拥有进程级：

```text
threading.Lock / task guard
```

所有触发来源都调用同一个：

```text
trigger(trigger_type, user_id=None)
```

如果已有任务运行：

- 定时触发：记录 skip，不重复启动；
- startup 触发：不重复启动；
- 管理员 API：返回 HTTP 409，并返回当前 active `run_id`。

不能允许：

```text
一个手动同步
+
一个定时同步
```

同时推进水位。

---

# 48. Job 实现方式

沿用项目现有后台任务模式。

建议新增：

```text
app/jobs/history_sync.py
```

主要职责：

- `start()`；
- `stop()`；
- 定时 loop；
- startup catch-up；
- single-flight；
- 调用 `asyncio.to_thread(history_sync_service.run, ...)`；
- 接入 `JobStatusService`；
- 管理 shutdown cancellation event。

不新增第三方 scheduler。

## 48.1 为什么用 `asyncio.to_thread`

Tushare SDK、SQLAlchemy/DuckDB 当前路径以同步调用为主。

与现有 Job 保持一致：

```text
FastAPI event loop
    ↓
asyncio.to_thread
    ↓
同步 HistorySyncService
```

避免长同步工作阻塞 Web 请求。

---

# 49. Shutdown 与进程中断

Python 无法安全强杀已经进入 `to_thread` 的同步函数。

因此 Service 接受：

```text
threading.Event cancellation_event
```

在以下节点检查：

- 每个交易日开始前；
- 每次重试 sleep 前后；
- 每个 master shard 之间。

如果收到停止：

1. 当前数据库事务允许正常完成；
2. 不开始下一交易日；
3. 当前 run 标记为 `INTERRUPTED` 或由下次启动恢复。

---

# 50. Stale RUNNING 恢复

应用启动时：

```text
SELECT history_sync_run
WHERE status = 'RUNNING'
```

如果存在，说明上次进程非正常结束。

启动恢复：

```text
run.status = INTERRUPTED
finished_at = now
```

并把仍处于：

```text
SYNCING
RETRYING
CHECKING
```

的 dataset state 恢复成基于水位的：

```text
LAGGING
或
CAUGHT_UP
```

然后 startup catch-up 再按 watermark 正常继续。

绝不能通过“run 曾经 RUNNING”猜测某个日期已经完成。

事实依据仍是：

```text
history_day_status + latest_complete_trade_date
```

---

# 51. 状态枚举

## 51.1 Dataset status

建议固定：

```text
UNINITIALIZED
CHECKING
SYNCING
RETRYING
CAUGHT_UP
LAGGING
FAILED
WAITING_SOURCE
```

## 51.2 Run status

```text
RUNNING
SUCCESS
PARTIAL
FAILED
INTERRUPTED
NOOP
```

## 51.3 错误码

至少标准化：

```text
TUSHARE_TOKEN_MISSING
TUSHARE_PERMISSION_DENIED
TUSHARE_RATE_LIMIT
TUSHARE_TIMEOUT
TUSHARE_API_ERROR

EMPTY_RESULT
TRUNCATION_RISK
SCHEMA_MISMATCH
DUPLICATE_KEY
TRADE_DATE_MISMATCH
UNKNOWN_INSTRUMENT
INVALID_VALUE

CALENDAR_UNAVAILABLE
DATABASE_ERROR
INTERNAL_ERROR
```

管理员页面显示：

- 简明错误码；
- 截断后的 human-readable message。

禁止把：

- Token；
- 完整敏感请求对象；
- 认证配置；

写入错误文本。

---

# 52. Admin API

新增：

```text
app/api/admin_history.py
```

路由统一：

```text
/api/admin/history-data
```

必须使用现有管理员认证依赖。

## 52.1 GET `/api/admin/history-data/summary`

返回：

```json
{
  "overall_status": "HEALTHY",
  "history_start_date": "2010-01-01",
  "latest_market_trade_date": "2026-09-16",
  "active_run": null,
  "daily_datasets": [],
  "master_datasets": []
}
```

每个日级数据集包含：

```text
dataset
display_name
status
history_start_date
data_min_date
data_max_date
latest_complete_trade_date
latest_expected_trade_date
next_trade_date
lag_trade_days
record_count
current_trade_date
current_attempt
last_success_at
last_error_code
last_error
```

## 52.2 POST `/api/admin/history-data/sync`

行为：

```text
管理员点击“检查并更新”
```

响应：

### 成功启动

```http
202 Accepted
```

```json
{
  "run_id": "...",
  "status": "RUNNING"
}
```

### 已有任务

```http
409 Conflict
```

```json
{
  "run_id": "...",
  "status": "RUNNING",
  "message": "历史数据同步正在运行"
}
```

不要让 HTTP 请求等待整个历史回填结束。

## 52.3 GET `/api/admin/history-data/runs`

参数：

```text
limit=20
```

返回最近任务。

## 52.4 GET `/api/admin/history-data/runs/{run_id}`

返回：

- run；
- 每个 dataset 的执行详情。

供页面轮询当前进度。

---

# 53. Admin 页面

新增：

```text
/admin/data
```

模板：

```text
app/templates/admin_data.html
```

页面必须使用：

```text
require_admin_page
```

并包含现有 CSRF token meta。

---

# 54. Admin 页面结构

## 54.1 顶部总体卡

显示：

```text
整体状态
历史起点
最新市场交易日
当前任务
最后一次执行
```

按钮：

```text
[ 检查并更新数据 ]
```

运行时：

```text
[ 正在更新... ]
```

并禁用按钮。

## 54.2 四个日级数据集卡片

固定：

```text
日线行情 daily
复权因子 adj_factor
每日指标 daily_basic
资金流 moneyflow
```

显示：

- 状态；
- 数据范围；
- 连续水位；
- 当前目标；
- 落后交易日；
- 总记录数；
- 当前处理日期；
- 当前 attempt；
- 最后成功；
- 最后错误。

## 54.3 主档状态

表格显示：

```text
stock_basic
trade_cal
namechange
stock_company
```

字段：

- 状态；
- 记录数；
- 上次成功刷新；
- bootstrap/cursor；
- 最后错误。

## 54.4 当前任务

展示：

```text
run_id
trigger
started_at
current dataset
current trade_date
attempt
本次 dates_completed
本次 rows_written
```

## 54.5 最近任务

最近 20 条：

```text
开始时间
触发方式
整体结果
耗时
各 dataset 推进
错误摘要
```

---

# 55. 前端交互

继续使用项目现有原生 JS。

建议在：

```text
app/static/app.js
```

增加：

```text
initAdminDataPage()
```

或在现有结构允许的情况下拆出小型 `admin_data.js`，但不要引入新前端构建系统。

## 55.1 轮询

任务运行中：

```text
每 3～5 秒刷新 summary / active run
```

任务结束：

- 停止高频轮询；
- 页面保持最终状态；
- 用户可以手动刷新。

不要一直每 3 秒扫大型事实表；API 只读取 sync 小表。

## 55.2 CSRF

POST 手动同步使用现有：

```text
X-CSRF-Token
```

机制。

不得新增绕过 CSRF 的管理员写接口。

---

# 56. 系统状态页集成

现有 `/admin/status` 已展示 JobStatus。

新增：

```text
history_sync
```

到 JobStatus 体系。

`JobStatusService` 只负责高层：

```text
最近开始
最近成功
最近失败
耗时
连续失败次数
```

详细历史进度不挤进 `job_status`，由：

```text
history_sync_*
```

表负责。

两套职责明确：

```text
JobStatus       = 系统任务健康
History Sync DB = 历史数据业务进度
```

---

# 57. 配置设计

建议 `config.example.yaml` 增加：

```yaml
history:
  enabled: true
  start_date: "2010-01-01"
  schedule_time: "20:30"
  startup_catchup: true

  max_attempts: 10

  request_min_interval_seconds: 0.6
  backoff_initial_seconds: 5
  backoff_max_seconds: 300
  jitter_ratio: 0.2

  stock_basic_refresh_hours: 24
  master_refresh_days: 7
```

可用时间 cutoff 建议集中定义在一个 Availability 配置模型/常量中：

```yaml
history:
  availability:
    adj_factor: "09:30"
    daily: "16:30"
    daily_basic: "17:30"
    moneyflow: "20:30"
```

## 57.1 Timezone

不增加任意 timezone 配置。

统一复用项目：

```text
BUSINESS_TZ_NAME = "Asia/Shanghai"
```

## 57.2 Tushare Token

继续：

```yaml
tushare:
  token: "..."
```

不新增：

- Token 数据库表；
- Admin Token 编辑页；
- API 返回 Token；
- 环境变量第二套来源。

---

# 58. 代码目录建议

建议最终形成：

```text
app/
├── api/
│   └── admin_history.py
│
├── jobs/
│   └── history_sync.py
│
├── models/
│   ├── history_market.py
│   └── history_sync.py
│
├── providers/
│   ├── base.py              # 扩展：历史内部模型 + HistoricalMarketDataProvider
│   ├── tushare_common.py    # Tushare client / RequestGate，共享 transport helper
│   ├── history/
│   │   ├── __init__.py      # HistoryProviderRegistry
│   │   └── tushare.py       # TushareHistoricalMarketDataProvider
│   └── trading_calendar/
│       └── provider.py      # 扩展 strict range 能力，禁止另建历史 trade_cal Provider
│
├── repositories/
│   ├── history_fact.py
│   ├── history_master.py
│   └── history_sync.py
│
├── schemas/
│   └── history_admin.py
│
├── services/
│   └── history/
│       ├── __init__.py
│       ├── availability.py
│       ├── validation.py
│       ├── planner.py
│       └── sync_service.py
│
├── templates/
│   └── admin_data.html
│
└── static/
    └── app.js
```

允许 Claude Code根据当前项目实际命名做轻微调整，但职责不要混乱。

---

# 59. 模块职责

## `providers/base.py`

在现有文件中增加：

- 历史数据内部标准模型；
- `ProviderBatch`；
- `HistoricalMarketDataProvider` Protocol。

不把 Tushare DataFrame 或 Tushare 专属 SDK 类型放进通用模型。

## `providers/history/__init__.py`

实现 `HistoryProviderRegistry`：

- 配置选源；
- Provider 构造；
- `call_with_metrics`；
- timeout；
- 历史 endpoint metrics key。

## `providers/history/tushare.py`

只负责 Provider 层：

- 调 Tushare；
- fields；
- 参数；
- RequestGate；
- Tushare schema 基础校验；
- normalize；
- `ts_code` → `instrument_id`；
- 返回内部 `ProviderBatch`。

## `providers/trading_calendar/provider.py`

复用并扩展现有实现：

- 保留实时市场状态 fallback；
- 增加 strict range 模式；
- 历史同步 strict 模式禁止任何 weekday approximation。

## `services/history/validation.py`

只处理已经标准化后的内部模型：

- 日期一致性；
- duplicate；
- 截断风险；
- 数值业务规则；
- instrument consistency；
- dataset-specific quality rules。

## `services/history/planner.py`

负责：

- watermark → pending dates；
- target date；
- lag trade days；
- reconcile continuous ledger。

## `repositories/history_fact.py`

负责：

- 某 dataset 某天查询；
- delete day；
- bulk insert；
- old row count；
- 必要的事实表 read。

## `repositories/history_master.py`

负责：

- stock basic；
- instrument merge；
- company；
- name change；
- strict calendar。

## `repositories/history_sync.py`

负责：

- state；
- day ledger；
- run；
- run dataset。

## `services/history/sync_service.py`

负责总编排。

它是：

```text
定时任务
管理员手动
startup catch-up
```

唯一共同业务入口。

---

# 60. Alembic Migration 方案

Claude Code 不应假设 migration 一定叫：

```text
0003
```

实施时首先执行：

```bash
alembic heads
```

基于当前实际 head 创建 migration。

## 60.1 Migration 内容

1. 扩展 `trading_calendar`；
2. 创建：
   - `cn_stock_basic`
   - `cn_stock_company`
   - `cn_stock_name_change`
3. 创建四张事实表；
4. 创建四张 sync/run 表；
5. 不修改或删除 `fundamental_snapshot`；
6. 不迁移旧 fundamental 数据进新表；
7. 不破坏已有 instrument / watchlist / user 数据。

## 60.2 Migration 兼容

`trading_calendar` 新增列先允许 NULL。

现有 CalendarRepository 仍可以只写：

```text
market
trade_date
is_open
```

历史严格 Calendar Repository 再主动填：

```text
exchange
pretrade_date
source
fetched_at
```

后续若所有旧路径都升级完，再考虑强化 NOT NULL，不属于本阶段强制内容。

## 60.3 升级前备份

保持项目现有 DuckDB 升级惯例。

生产/真实数据升级前：

```bash
cp data/marketmind.duckdb data/marketmind.duckdb.bak
```

Docker 路径按部署实际处理。

---

# 61. Schema Evolution

Tushare 未来可能新增字段。

原则：

```text
Provider 显式 fields
+
DB 显式 schema
```

因此不会自动把新字段吞进数据库。

增加新字段流程：

```text
确认 Tushare 字段语义
    ↓
产品决定是否保存
    ↓
Alembic migration
    ↓
Core table / Model 增列
    ↓
Provider fields
    ↓
Validator
    ↓
Tests
```

禁止通过：

```text
JSON blob 存所有未知列
```

绕过 schema 管理。

---

# 62. 数据源错误与可观测性

每次 Provider 调用继续接入现有 metrics 体系或新增同样风格的 metrics。

建议 provider 名：

```text
tushare_history_stock_basic
tushare_history_daily
tushare_history_adj_factor
tushare_history_daily_basic
tushare_history_moneyflow
...
```

如果当前 Metrics 设计更适合按 provider 聚合，可统一：

```text
tushare_history
```

并把 endpoint 放日志字段。

不要为了 metrics 重构整个 observability 模块。

日志必须包含：

```text
run_id
dataset
trade_date
attempt
row_count
elapsed_ms
error_code
```

禁止日志包含 Token。

---

# 63. 日志级别建议

正常每日完成：

```text
INFO
```

重试：

```text
WARNING
```

10 次失败：

```text
ERROR
```

单次几千行事实不逐行打印。

首次回填日志建议每个交易日一条概要即可。

---

# 64. 与现有 FundamentalRefreshJob 的并存

第一阶段：

```text
FundamentalRefreshJob
```

继续工作。

它仍负责：

- 当前自选 A 股；
- 当前页面所需 PE(TTM)/PB/股息率；
- 原 `fundamental_snapshot`。

新：

```text
HistorySyncJob
```

负责：

- A 股全市场；
- 十多年；
- 全字段；
- 连续水位；
- 长期分析事实。

二者共享 Tushare RequestGate。

未来若要淘汰旧 Job，必须单独做行为迁移与 UI 回归测试，不在本方案中顺手删除。

---

# 65. 与现有 TradingCalendarProvider 的并存

现有实时行情“是否开市”逻辑可以保留原有容错策略。

历史同步增加“strict mode”：

```text
严格 Tushare calendar
失败即失败
无 fallback
```

建议不要直接把现有所有场景改成 strict。

可以：

```text
现有 TradingCalendarProvider
    → 实时市场状态

HistoryCalendarRepository/Service
    → 历史数据连续性
```

底层仍共用 `trading_calendar` 表。

---

# 66. 数据查询接口暂不扩张

第一阶段管理员 API 只提供：

- 数据健康；
- run 状态；
- 手动同步。

暂不新增公开：

```text
/api/history/kline
/api/history/valuation
/api/history/moneyflow
```

这些属于下一阶段消费能力。

但是 Repository 设计时应避免把事实表写死成“只能导入不能查询”。

---

# 67. 安全要求

1. `/admin/data` 仅管理员；
2. `/api/admin/history-data/*` 仅管理员；
3. POST 必须 CSRF；
4. 手动 run 的 `requested_by_user_id` 从当前认证用户服务端取得；
5. 不接受客户端传任意 user_id；
6. Tushare Token 不进入页面；
7. Tushare Token 不进入 API；
8. 错误文本过滤敏感配置；
9. 数据表为全局共享，不增加 user_id；
10. 普通用户不能触发历史同步。

---

# 68. 测试设计

必须继续满足：

```bash
pytest
```

默认完全离线。

网络测试只放：

```text
@pytest.mark.online
```

---

# 69. Unit Tests

至少包括：

## 69.1 Availability

- 交易日上午；
- 收盘后；
- daily cutoff 前后；
- daily_basic cutoff 前后；
- moneyflow cutoff 前后；
- 周末；
- 节假日；
- Asia/Shanghai 与服务器时区无关。

## 69.2 Planner

- watermark NULL；
- watermark 已有；
- 一年 backlog；
- 无 pending date；
- 中间交易日列表顺序；
- 不使用 `date + 1`；
- reconcile 找到第一缺口。

## 69.3 Retry

通过注入：

```text
sleep function
random function
```

测试：

- 第一次成功；
- 第 3 次成功；
- 第 10 次成功；
- 10 次全部失败；
- delay cap；
- jitter。

不要让测试真的 sleep。

## 69.4 Validator

覆盖：

- 缺 required column；
- trade_date 不一致；
- duplicate ts_code；
- 6000 截断；
- 空结果；
- unknown instrument；
- daily 合法 NULL；
- daily_basic PE NULL；
- adj_factor <= 0；
- moneyflow net negative 合法。

---

# 70. Integration Tests：DuckDB

必须使用真实临时 DuckDB。

## 70.1 首次三天同步

Mock Provider：

```text
2010-01-04
2010-01-05
2010-01-06
```

验证：

- 三天事实；
- 三天 ledger；
- watermark = 01-06；
- state record_count 正确。

## 70.2 中间失败绝不跳过

Mock：

```text
01-04 success
01-05 fail × 10
01-06 provider 若被调用则测试失败
```

验证：

```text
watermark = 01-04
01-06 从未请求
```

这是最重要的回归测试。

## 70.3 下一次从失败日恢复

第二次运行：

```text
01-05 success
01-06 success
```

验证最终连续追平。

## 70.4 Dataset 独立

```text
daily success
daily_basic fail
adj_factor success
moneyflow success
```

验证其他三者正常推进。

## 70.5 事务回滚

人为在：

```text
DELETE 后
INSERT 后
state update 前
```

注入异常。

验证：

- 旧事实没有丢；
- ledger 没错误推进；
- watermark 没推进。

## 70.6 重复运行

同一天运行两次。

验证：

```text
row_count 不翻倍
```

## 70.7 6000 fallback

构造正好 6000 行。

验证：

- 不直接 commit；
- fallback 被调用；
- fallback 失败则 watermark 不动。

## 70.8 Stale run 恢复

数据库预置：

```text
run = RUNNING
state = SYNCING
```

模拟重启。

验证：

```text
旧 run → INTERRUPTED
startup catch-up 从 watermark 继续
```

## 70.9 Manual 与 Scheduled single-flight

同时触发。

验证：

- 只有一个 run；
- 第二次返回 busy；
- 不出现双写。

---

# 71. Migration Tests

从当前 migration head 建立旧数据库状态，预置：

- instrument；
- fundamental_snapshot；
- watchlist；
- user；
- trading_calendar；
- job_status。

执行：

```bash
alembic upgrade head
```

验证：

- 旧数据数量不变；
- 新表存在；
- trading_calendar 增列；
- 应用模型可查询；
- downgrade（如果项目要求）至少不会破坏未涉及旧表。

---

# 72. API Tests

至少：

```text
GET summary 未登录
GET summary 普通用户
GET summary 管理员

POST sync 未登录
POST sync 普通用户
POST sync 管理员无 CSRF
POST sync 管理员有效 CSRF
POST sync 任务已运行 → 409

GET runs
GET run detail
```

沿用项目当前认证测试习惯。

---

# 73. Online Smoke Tests

标记：

```text
@pytest.mark.online
```

仅做小规模验证。

建议：

1. `stock_basic` 一个 shard；
2. `trade_cal` 一个小日期窗口；
3. 最近一个已完成交易日：
   - daily
   - adj_factor
   - daily_basic
   - moneyflow
4. company 一个 exchange；
5. namechange 一个已知证券。

验证：

- 权限；
- 字段；
- DataFrame schema；
- `fields` 参数；
- 日期格式；
- BSE `ts_code` 实际格式；
- 返回上限行为。

Online test 不写生产 DuckDB。

---

# 74. 性能测试

增加一个本地 synthetic benchmark：

```text
6000 rows × 100 trade days
```

测试：

- Core batch insert；
- 整日 delete + insert；
- state update；
- 事实表基础 date filter。

目标不是设一个未经 benchmark 的绝对毫秒 SLA。

目标是确认：

> 实现没有退化成逐行 ORM / 每行一次 commit。

---

# 75. Claude Code 实施顺序

为了减少一次性改动范围，建议严格分阶段。

## Phase 0：Preflight

Claude Code 首先：

```bash
git status
alembic heads
pytest
```

并检查：

- 当前 migration head；
- `app/main.py`；
- `app/db.py`；
- 当前 Job 实现；
- Admin 路由认证方式；
- 当前 CSRF 前端调用；
- Instrument schema；
- trading_calendar schema。

如果仓库在本方案之后已经发生变化：

> 以当前代码为准调整文件位置，但保留本文业务不变量。

---

## Phase 1：数据库 schema

实现：

- migration；
- master models；
- history sync models；
- fact Core tables；
- schema migration tests。

暂不接 Tushare。

验收：

```bash
pytest
```

通过。

---

## Phase 2：复用并扩展现有 Provider 框架

实现顺序：

1. 阅读并复用 `app/providers/base.py`；
2. 阅读并仿照 `QuoteProviderRegistry`；
3. 复用 `ProviderMetricsRegistry/call_with_metrics`；
4. 在 `AppConfig.providers` 中增加 history Provider 配置；
5. 新增 `TushareRequestGate` / Tushare client helper；
6. 让现有 Tushare Fundamental / Calendar Provider 逐步共享 transport helper；
7. 在现有 `base.py` 增加历史内部模型和 Protocol；
8. 新增 `HistoryProviderRegistry`；
9. 新增 `TushareHistoricalMarketDataProvider`；
10. 扩展现有 `TushareTradingCalendarProvider` 的 strict range 模式；
11. 实现全字段列表和 Tushare→内部模型转换；
12. provider mock tests；
13. 小型 online smoke tests。

不要新增 `providers/history/base.py`，不要另建 metrics/timeout 框架，也不要先实现调度器。

---

## Phase 3：Repository + Validator

实现：

- Master Repository；
- Fact Repository；
- Sync Repository；
- Validation；
- 整日 delete + batch insert；
- 截断检测；
- instrument mapping；
- repository integration tests。

---

## Phase 4：HistorySyncService

实现：

- Watermark planner；
- reconcile；
- strict calendar；
- retry；
- master prerequisites；
- 四个日级 dataset；
- run / run_dataset；
- stale run recover；
- cancellation。

先用测试直接调用：

```python
service.run(...)
```

验证所有连续性场景。

这是整个项目最关键阶段。

---

## Phase 5：HistorySyncJob

实现：

- scheduled 20:30；
- startup catch-up；
- asyncio.to_thread；
- single-flight；
- shutdown；
- JobStatus。

接入 FastAPI lifespan。

---

## Phase 6：Admin API

实现：

- summary；
- sync；
- runs；
- run detail；
- admin auth；
- CSRF；
- 409 busy。

---

## Phase 7：Admin 页面

实现：

- `/admin/data`；
- 顶部状态；
- 四个 dataset card；
- master 状态；
- current run；
- recent runs；
- manual sync；
- polling；
- Admin 导航入口。

保持现有 UI 风格。

---

## Phase 8：回归与文档

执行：

```bash
pytest
pytest -m online
```

更新：

- README；
- config.example.yaml；
- CHANGELOG；
- 必要部署说明。

在真实数据首次执行前备份 DuckDB。

---

# 76. Claude Code 实施约束

把以下内容作为实施时的硬约束。

## 76.1 不要做的事情

Claude Code 不得：

- 新建一套平行的 Provider 基础框架；
- 新建 `app/providers/history/base.py` 取代现有 `app/providers/base.py`；
- 让 Tushare DataFrame / SDK 对象穿透 Provider 边界进入 Service；
- 新建历史专属 metrics / timeout 系统；
- 新建第二个 trade_cal Provider；
- 为历史任务引入 Celery / Redis；
- 修改部署为多 worker；
- 让网络请求进入 WriteCoordinator；
- 直接用 `MAX(trade_date)` 作为同步水位；
- 失败后 `continue` 到下一个交易日；
- 把 qfq / hfq 再建两张长期事实表；
- 删除现有 FundamentalRefreshJob；
- 把 `fundamental_snapshot` 直接改造成全市场历史表；
- 使用周一～周五 fallback 推进历史连续水位；
- 在管理员页面暴露 Token；
- 给大型事实表逐行 ORM insert；
- 假定所有已上市证券每天都有 daily；
- 把估值 NULL 自动变成 0；
- 因为接口空结果就自动标 COMPLETE；
- 未经验证擅自假设 Tushare 支持批量多代码参数；
- 在单元测试中访问真实网络；
- 改动与历史数据无关的大量项目结构。

## 76.2 必须做的事情

Claude Code 必须：

- 先阅读现有 `app/providers/base.py`、`app/providers/quote/__init__.py`、`app/observability/provider_metrics.py`、`app/providers/trading_calendar/provider.py`；
- 复用现有 Provider Protocol/Registry/config/metrics/timeout 体系；
- 历史 Provider 通过内部标准模型与 Service 交互；
- 扩展现有 TradingCalendarProvider 支持 strict 历史模式；
- 先跑现有测试；
- 基于当前 Alembic head 创建 migration；
- 每一个主要 Phase 后跑测试；
- 四个 dataset 分别维护连续水位；
- 事务中同时完成事实数据、day ledger、水位更新；
- 为中间日期失败写集成测试；
- 为重启恢复写集成测试；
- 为 6000 行截断写测试；
- 手动与自动任务使用同一个 Service；
- 保证 Admin POST CSRF；
- 使用 Asia/Shanghai；
- 保留所有可获取原始业务字段；
- 保留退市证券。

---

# 77. 首次真实部署步骤

推荐：

```text
1. 停止服务
2. 备份 marketmind.duckdb
3. 更新代码
4. 更新 config.yaml / config.example.yaml 对应配置
5. alembic upgrade head
6. pytest（部署前环境可选但建议）
7. 启动单 worker 应用
8. 管理员打开 /admin/data
9. 确认 master 数据状态
10. 触发或等待首次历史同步
```

首次回填过程中：

- 页面持续展示进度；
- 任务可跨多次启动完成；
- 达到 Tushare 限制或临时失败时停在当前水位；
- 下次运行继续；
- 不要求一次进程生命周期内强行跑完 2010→当前。

---

# 78. 数据一致性检查工具

除自动同步外，建议 Service 提供内部：

```text
reconcile_dataset(dataset)
```

但第一阶段不必在管理员页面增加“修复/重建”按钮。

它负责：

1. 检查 strict calendar；
2. 扫描 `history_day_status` 连续性；
3. 对照 watermark；
4. 检查事实最大日期是否异常超前；
5. 保守回退错误水位；
6. 返回诊断报告。

管理员页面的“检查并更新”内部可以自动调用。

不允许管理员直接手工输入水位日期。

---

# 79. 事实表出现“水位之后的数据”怎么办

理论上正常流程不会出现。

但旧脚本、人工 SQL 或异常版本可能造成：

```text
watermark = 2025-09-01
fact max = 2025-09-10
```

处理原则：

> **不相信更晚事实意味着中间完整。**

Planner 仍然从：

```text
2025-09-02
```

开始。

当逐日推进到已有的 2025-09-10：

- 当日整日替换；
- 重新建立 day ledger；
- 正常推进。

不需要先删除所有“超前数据”。

这样恢复最安全。

---

# 80. 数据源迟到修订

第一阶段的连续同步不会每天重抓所有过去日期。

因此对 Tushare 之后修订很久以前的数据，不主动全库重刷。

但是技术实现的“整日替换”天然支持未来增加：

```text
resync date range
```

功能。

第一阶段管理员 UI 不开放该功能，避免误操作。

未来若需要，可以增加：

```text
管理员高级操作：
重新同步某日期范围
```

但仍必须保持主连续水位不被错误跳转。

---

# 81. 数据规模与存储预期

从 2010 至今：

- 交易日约数千；
- A 股证券数量从早期较少增长到当前数千；
- 四个全市场日级事实数据集总记录数将达到较大规模。

因此设计已经避免：

- 每行 UUID run_id；
- 不必要的大表 ORM identity；
- 不必要的大表 FK；
- 管理员实时 `COUNT(*)`；
- 每行 transaction；
- 每证券一个永久 sync state。

同步状态是：

```text
按 dataset
+
按 dataset/day ledger
```

而不是：

```text
几千证券 × 四数据集 × 每日状态
```

---

# 82. 为什么不做“每只股票一个水位”

本需求的获取模型是：

```text
按交易日获取全市场
```

所以水位应该是：

```text
dataset + trade_date
```

而不是：

```text
dataset + instrument_id + trade_date
```

这样：

- 状态表小；
- 容易证明日期连续；
- 与 Tushare 推荐的全市场按日期获取方式一致；
- 避免数千万同步状态记录；
- 管理员页面更容易理解。

证券级自然缺失由数据集语义处理，不通过证券水位表达。

---

# 83. 为什么四个数据集不共享一个水位

因为：

- 更新时间不同；
- API 可靠性不同；
- 接口覆盖不同；
- moneyflow 可能暂时失败但 daily 正常；
- adj_factor 可能已经可用而 daily_basic 尚未完成。

所以：

```text
daily watermark
adj_factor watermark
daily_basic watermark
moneyflow watermark
```

必须独立。

统一任务只负责一次性编排它们。

---

# 84. 总体状态计算

管理员页面 `overall_status` 建议动态计算。

优先级：

```text
RUNNING
    如果有 active run

ERROR
    如果任意核心 dataset = FAILED

LAGGING
    如果无 FAILED，但任意核心 dataset watermark < target

WAITING
    如果仅存在 WAITING_SOURCE，且未超出合理发布时间

HEALTHY
    四个核心 dataset 全部 CAUGHT_UP
```

主档 `stock_company/namechange` 的短暂失败可以显示 warning，但不一定把整个历史行情标记 ERROR。

`trade_cal/stock_basic` 失败属于核心前置错误，应提升整体异常级别。

---

# 85. 关键设计决策摘要

| 编号 | 决策 |
|---|---|
| D1 | 日级历史基准起点固定 2010-01-01 |
| D2 | 四个日级数据集分别维护独立连续水位 |
| D3 | 水位不等于 MAX(trade_date) |
| D4 | 失败日期不可跳过 |
| D5 | 一天一个原子事务 |
| D6 | 事实写入 + day ledger + watermark 同事务 |
| D7 | 网络请求永远在 WriteCoordinator 之外 |
| D8 | 整日 DELETE + batch INSERT 保证幂等与修订一致 |
| D9 | 大事实表使用 Core 批量写，不逐行 ORM |
| D10 | 原始 daily + adj_factor，不长期重复存 qfq/hfq |
| D11 | 现有 fundamental_snapshot 暂时保留为 serving cache |
| D12 | history calendar 禁止工作日 fallback |
| D13 | Tushare 所有调用共享全局 RequestGate |
| D14 | 6000 行等于潜在截断，不允许直接推进水位 |
| D15 | stock_basic 按 exchange × list_status 分片 |
| D16 | stock_company 按 exchange 分片 |
| D17 | scheduled/manual/startup 共用 HistorySyncService |
| D18 | 统一任务 single-flight |
| D19 | 默认每天 20:30 Asia/Shanghai |
| D20 | 启动时自动 catch-up |
| D21 | 管理员页读取 sync 小表，不实时扫描全事实表 |
| D22 | Master 与 daily facts 使用不同进度语义 |
| D23 | 退市证券永久保留，不因退市删除历史身份 |
| D24 | Tushare Token 沿用 config.yaml，不进入 DB/UI |
| D25 | 不新增 Celery/Redis/第二数据库 |
| D26 | 历史数据扩展现有 Provider 框架，不建立第二套 Provider 基础设施 |
| D27 | HistoryProviderRegistry 复用 AppConfig + ProviderMetricsRegistry + call_with_metrics |
| D28 | Tushare 原始 DataFrame 不得越过具体 Provider 边界 |
| D29 | trade_cal 复用并扩展现有 TushareTradingCalendarProvider 的 strict 模式 |

---

# 86. 第一阶段 Definition of Done

## 数据库

- [ ] 新 master 表创建完成；
- [ ] 四张日级事实表创建完成；
- [ ] 四张同步控制表创建完成；
- [ ] trading_calendar 扩展完成；
- [ ] 现有业务表未破坏。

## Provider

- [ ] 历史 Protocol/内部模型扩展在现有 `app/providers/base.py`；
- [ ] `HistoryProviderRegistry` 复用现有 Registry 模式；
- [ ] 历史 Provider 调用复用 `ProviderMetricsRegistry/call_with_metrics`；
- [ ] Tushare DataFrame 不穿透 Provider 边界；
- [ ] trade_cal strict 由现有 `TushareTradingCalendarProvider` 扩展提供；
- [ ] stock_basic 全状态可取；
- [ ] trade_cal strict 可取；
- [ ] stock_company 可取；
- [ ] namechange 可取；
- [ ] daily 全字段可取；
- [ ] adj_factor 可取；
- [ ] daily_basic 全字段可取；
- [ ] moneyflow 全字段可取；
- [ ] Tushare RequestGate 生效。

## 同步

- [ ] 2010 起点；
- [ ] 四个独立水位；
- [ ] 严格交易日推进；
- [ ] 中间失败不跳日；
- [ ] 10 次重试；
- [ ] 下次从失败日恢复；
- [ ] 整日原子提交；
- [ ] 重复执行幂等；
- [ ] 6000 截断不误判完整；
- [ ] 空结果不误推进；
- [ ] master 前置工作正常；
- [ ] stale run 可恢复。

## Job

- [ ] 每天 20:30 Asia/Shanghai；
- [ ] startup catch-up；
- [ ] single-flight；
- [ ] graceful shutdown；
- [ ] JobStatus 集成。

## Admin

- [ ] `/admin/data`；
- [ ] summary；
- [ ] 四个 dataset 状态；
- [ ] master 状态；
- [ ] current run；
- [ ] recent runs；
- [ ] 手动同步；
- [ ] busy 409；
- [ ] Admin 权限；
- [ ] CSRF。

## Tests

- [ ] 默认 `pytest` 无网络通过；
- [ ] migration tests；
- [ ] middle-date failure test；
- [ ] atomic rollback test；
- [ ] restart recovery test；
- [ ] truncation test；
- [ ] online smoke tests 可单独运行。

---

# 87. 建议 Claude Code 开始实施时使用的任务描述

可将下面这段直接作为 Claude Code 的实施入口：

```text
请依据仓库根目录中的
《MarketMind A股历史数据产品设计与数据说明》
和
《MarketMind A股历史数据技术方案》
实现 A 股历史数据第一阶段。

要求：

1. 先阅读现有 README、stocksview_duckdb_technical_plan.md、app/db.py、
   app/main.py、app/providers/base.py、app/providers/quote/__init__.py、
   app/providers/fundamental/tushare.py、app/providers/trading_calendar/provider.py、
   app/observability/provider_metrics.py、现有 jobs、auth/admin API、
   Alembic migrations 和 tests。
2. 先运行 git status、alembic heads、pytest，确认现状。
3. 历史数据必须扩展现有 Provider 框架：复用 base Protocol/内部模型、
   Registry、AppConfig、ProviderMetricsRegistry/call_with_metrics；
   不得新建一套平行 Provider 基础设施。
4. 历史 Provider 不得把 Tushare DataFrame/SDK 对象传入 Service；
   必须在 Provider 内转换成 MarketMind 内部标准模型。
5. trade_cal 必须扩展复用现有 TushareTradingCalendarProvider，
   History 使用 strict 模式，严禁 weekday fallback。
6. 严格按技术方案 Phase 0→8 分阶段实现。
7. 每个阶段完成后运行相关测试；不要一次性重写整个项目。
8. 四个日级数据集必须保持独立连续水位，任何失败日期不得跳过。
9. 事实数据、history_day_status、水位推进必须位于同一个 DB 事务。
10. 所有网络请求必须在 WriteCoordinator 锁外。
11. 不引入 Redis/Celery/新数据库/前端框架。
12. 不删除现有 FundamentalRefreshJob，不把 fundamental_snapshot
    直接改成全市场历史表。
13. 默认测试禁止真实网络；真实 Tushare 只放 @pytest.mark.online。
14. 不硬编码 migration 编号，基于实际 alembic head 创建。
15. 如当前仓库结构与技术方案文件名略有变化，可适配当前结构，
    但不得改变产品不变量。
```

---

# 88. 参考资料

## 产品设计

- 《MarketMind A股历史数据：产品设计与数据说明》v0.1

## MarketMind

- 项目  
  https://github.com/ilevin/MarketMind

- DuckDB 技术规划  
  https://github.com/ilevin/MarketMind/blob/main/stocksview_duckdb_technical_plan.md

## Tushare

- A股日线 `daily`  
  https://tushare.pro/document/2?doc_id=27

- 每日指标 `daily_basic`  
  https://tushare.pro/document/2?doc_id=32

- `pro_bar`  
  https://tushare.pro/document/2?doc_id=109

- 个股资金流 `moneyflow`  
  https://tushare.pro/document/2?doc_id=170

- 股票基础信息 `stock_basic`  
  https://tushare.pro/document/2?doc_id=25

- 交易日历 `trade_cal`  
  https://tushare.pro/document/2?doc_id=26

- 股票曾用名 `namechange`  
  https://tushare.pro/document/2?doc_id=100

- 上市公司基本信息 `stock_company`  
  https://tushare.pro/document/2?doc_id=112

- 复权因子 `adj_factor`  
  https://tushare.pro/document/2?doc_id=28

---

# 88.1 Provider 框架复用结论

本方案 v0.2 明确修订上一版中“另建 `providers/history/base.py`”的倾向。

最终规则为：

```text
现有 Provider Framework
        ↓ 扩展
HistoricalMarketDataProvider
        ↓
HistoryProviderRegistry
        ↓
TushareHistoricalMarketDataProvider
```

而不是：

```text
现有 Provider Framework

+ 一套独立 History Provider Framework
```

因此，未来如果增加第二个历史数据源，只需要：

1. 实现现有 `HistoricalMarketDataProvider` Protocol；
2. 在 `HistoryProviderRegistry` 登记；
3. 在 `config.providers.history` 选择；

HistorySyncService、水位算法、Repository、Admin 页面均无需感知具体数据源。

---

# 89. 最终结论

本技术方案把历史数据系统的可靠性建立在四个核心机制上：

```text
严格交易日历
+
每数据集独立连续水位
+
每日事实数据与水位原子提交
+
失败日绝不跳过
```

再通过：

```text
Tushare 全局限速
重试
截断识别
幂等整日替换
任务互斥
重启恢复
Admin 可观测
```

保证系统可以从 2010 年开始逐步建立并长期维护 A 股历史原始数据底座。

第一阶段完成后，MarketMind 可以在不重新建设数据基础层的前提下继续增加：

- 股票历史 K 线；
- qfq / hfq；
- 历史估值；
- 资金流分析；
- 条件选股；
- 因子研究；
- 策略回测；
- AI 历史数据分析。

技术实现时最优先守住的不是“下载速度”，而是：

> **已经推进的每一个连续水位，都必须能够代表该数据集从 2010 起至该日的所有应处理交易日已经按规则成功完成。**
