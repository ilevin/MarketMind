# etf-quant-api Specification

## Purpose
TBD - created by archiving change etf-data-module. Update Purpose after archive.
## Requirements
### Requirement: ETF 统一查询接口 get_etf_daily

系统 SHALL 在 `app/services/quant/` 提供统一 ETF 历史行情查询接口 `get_etf_daily(symbol, start_date, end_date, adjust)`：symbol 为 6 位 ETF 代码（如 `510300`），内部经 `instrument` 表（`CN:ETF:<symbol>`）定位；adjust SHALL 仅接受 `raw`/`qfq`/`hfq`（默认 raw）。raw SHALL 直读 `etf_daily` 区间行；qfq/hfq SHALL 按"复权计算与因子基准"在查询时动态计算。返回 SHALL 为按 trade_date 升序的标准行情序列（trade_date、open、high、low、close、volume、amount、turnover_rate），数值 SHALL 保持原始单位（与事实表口径一致，不在查询层换算）。symbol 无法定位到 ETF instrument 时 SHALL 抛出明确错误（含代码与排查提示），SHALL NOT 静默返回空序列。查询 SHALL NOT 写任何数据库状态。

#### Scenario: 原始行情查询

- **WHEN** 调用 `get_etf_daily("510300", date(2026,1,1), date(2026,9,30), adjust="raw")` 且区间内已有 180 个交易日数据
- **THEN** 返回 180 条升序记录，OHLC/成交量与 `etf_daily` 事实表逐行一致，无任何换算

#### Scenario: 未知代码明确报错

- **WHEN** 调用 `get_etf_daily("999999", …)` 且主档中不存在 CN:ETF:999999
- **THEN** 抛出明确错误（说明代码未入库、需先完成 ETF universe 同步），不返回空序列冒充成功

### Requirement: 复权计算与因子基准

qfq/hfq SHALL 在查询时基于 `etf_daily` 原始价与 `etf_adj_factor` 动态计算：后复权 `hfq_price = raw_price × factor_t`（factor_t 为该交易日复权因子）；前复权 `qfq_price = raw_price × factor_t / factor_latest`（factor_latest 为该 ETF 全库最新 adj_factor，与券商行情软件口径一致——当前价等于真实价）。复权取值 SHALL 封装在单点 `factor_at(trade_date)`：基线假设 fund_adj 每交易日一行；若在线实测确认上游仅除权事件日有行，SHALL 改为"取 ≤ 该交易日最近的因子行"（forward-fill），两种语义的实现只允许变化于该单点。复权价格 SHALL NOT 写入任何事实表或缓存表（复用既有"不存储派生复权数据"原则）；换手率、成交量、成交额 SHALL NOT 参与复权换算（仅价格字段复权）。历史区间的 qfq 结果随 factor_latest 滚动属预期行为，SHALL 在接口文档明示；hfq 不受基准日影响、可复现。

#### Scenario: 后复权逐日正确

- **WHEN** 某 ETF 2026-01-05 raw close=1.000、factor=1.500，2026-06-01 除权后 raw close=0.750、factor=2.000
- **THEN** 两日 hfq close 分别为 1.500 与 1.500（除权不产生跳空），qfq 以最新 factor=2.000 为基准时分别为 0.750 与 0.750

#### Scenario: 前复权基准为全库最新因子

- **WHEN** 查询区间为 2020 年、该 ETF 全库最新因子位于 2026-10-06
- **THEN** qfq 使用 2026-10-06 的因子作分母（而非区间末因子），查询区间最近交易日的前复权价与实时行情软件一致

#### Scenario: 复权结果永不入库

- **WHEN** 检查 get_etf_daily 的 qfq/hfq 查询路径
- **THEN** 只产生内存结果，不产生任何 qfq/hfq 事实表、缓存表或临时落库行为

### Requirement: 跨源日期一致性

qfq/hfq 查询 SHALL 校验"区间内日线存在的交易日，其复权因子可得"：因子缺失（etf_adj_factor 水位未追平或 fund_adj 不覆盖该 ETF）时 SHALL 抛出明确错误并列出缺失交易日（不静默回退 raw、不用相邻日因子外推掩盖缺口）；因子存在而日线缺失的交易日（停牌/未交易）SHALL 不产生该日行情输出，其因子 SHALL 仍参与 factor_latest 基准计算。raw 查询 SHALL NOT 因因子表状态受任何影响。因子表完全为空时 qfq/hfq SHALL 报"复权因子未同步"明确错误（提示检查 Tushare fund_adj 数据源），SHALL NOT 视作该 ETF 无复权需求。

#### Scenario: 因子缺失明确报错

- **WHEN** 某查询区间内 3 个交易日有日线但无对应 adj_factor 行，请求 adjust="qfq"
- **THEN** 抛出语义化错误并列出 3 个缺失交易日，不返回任何静默回退的数据

#### Scenario: 停牌日因子参与基准

- **WHEN** 某 ETF 在某交易日停牌（etf_daily 无行、etf_adj_factor 有行）
- **THEN** 该日不输出行情记录，该因子仍参与 factor_latest 计算

#### Scenario: raw 查询不受因子表影响

- **WHEN** etf_adj_factor 完全为空时请求 adjust="raw"
- **THEN** 正常返回全部日线原始数据

### Requirement: REST 查询端点

系统 SHALL 提供 `GET /api/quant/etf/daily?symbol=&start=&end=&adjust=` 端点（`app/api/quant.py`）：登录用户 SHALL 可用（普通登录用户即可，SHALL NOT 要求管理员——量化研究/AI Agent 场景）；未登录 SHALL 401。参数校验：symbol 必填（6 位数字）、adjust ∈ raw|qfq|hfq、start ≤ end、日期格式合法，非法参数 SHALL 422。响应 SHALL 包含 `{symbol, name, ts_code, instrument_id, adjust, items: [{trade_date, open, high, low, close, volume, amount, turnover_rate}, …]}`；空区间 SHALL 返回空 items（200）。qfq/hfq 因子缺失 SHALL 返回语义化错误响应（含缺失日明细）。单次查询区间 SHALL NOT 设行数硬上限（单 ETF 十六年约 4000 行，响应可控），文档 SHALL 写明单位口径。

#### Scenario: 登录用户查询

- **WHEN** 普通登录用户携带有效参数 GET /api/quant/etf/daily?symbol=510300&adjust=qfq
- **THEN** 返回 200 与复权行情序列，响应含 instrument 元信息与 items 列表

#### Scenario: 未登录拒绝

- **WHEN** 未登录用户请求该端点
- **THEN** 返回 401，不返回任何数据

#### Scenario: 参数校验

- **WHEN** 请求 adjust=xx 或 start > end 或 symbol 为空
- **THEN** 返回 422 与字段级错误说明，不触发数据库查询

