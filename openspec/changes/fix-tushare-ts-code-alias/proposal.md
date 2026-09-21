## Why

v0.3.0 线上首次回填在 2010-01-04 卡死：Tushare 历史事实接口返回 `000022.SZ`
（深赤湾A 的历史代码），而 `stock_basic` 主档只含当前代码 `001872.SZ`
（招商港口，2018-12-26 代码变更）。`daily` / `adj_factor` / `daily_basic`
三个数据集因此报 `UNKNOWN_INSTRUMENT` 并停在 2010-01-04，`moneyflow` 推进
到 2010-01-04 后在 2010-01-05 同样失败；水位永远无法推进，回填无法完成。

这不是部署、迁移、DuckDB、Token 或网络问题，而是**代码变更导致的历史身份
不一致**：

- 主档已按 3 交易所 × 5 上市状态取全部 15 个分片，旧代码在任何分片中都不
  存在——§35.1 规定的"刷新一次 stock_basic 后重映射"恢复路径**结构性无效**；
- Provider 用 `_symbol_of(ts_code)` 直接映射 instrument，没有别名/规范化层；
- 该情形会在历史上反复出现（每一次证券代码变更都会留下这样的旧代码），
  当前实现把每个这样的日子都变成永久阻塞点。

## What Changes

在 Tushare Provider 边界内新增**已登记** ts_code 别名规范化层：

- 新模块 `app/providers/history/tushare_aliases.py`：`TUSHARE_TS_CODE_ALIASES`
  登记表（首条 `000022.SZ -> 001872.SZ`）、`canonical_ts_code()`、
  `normalize_historical_aliases()`、`HistoricalAliasConflictError`
  （`error_code=ALIAS_CONFLICT`）；
- 接入两条日级抓取路径（全市场主路径 `_fetch_day_level` 与逐证券 fallback
  `_fetch_day_level_per_instrument`），四个日级数据集统一生效；
  `raw_row_count` 仍在规范化**前**取值，监控口径不变；
- 三种情形：仅旧代码 → 改写为规范代码；新旧并存且字段一致 → 保留规范代码
  行、丢弃旧代码行（WARNING 带 `action=drop_legacy`）；新旧并存但字段冲突
  → `ALIAS_CONFLICT` 失败、不推进水位；
- 冲突比较不用 `DataFrame.equals`：按现有 `_cell` 空值口径归一化、数值走
  极小相对容差、日期统一为 YYYYMMDD；
- `ALIAS_CONFLICT` 加入 `CONFIG_ERROR_CODES`：不会随重试自愈，快速终态失败；
- **不改**主档三个数据集（`stock_basic` / `stock_company` / `namechange`）：
  主档是 instrument 的来源，改写会造出第二条证券记录；
- 无数据库迁移。

## Capabilities

### New Capabilities

（无——别名层属既有 `history-provider` 能力内部的实现约束，不新建能力。）

### Modified Capabilities

- `history-provider`: 新增"历史 ts_code 别名规范化"要求——Provider SHALL 在
  映射前把已登记的历史代码改写为规范代码；新旧并存且一致时只保留规范行；
  字段冲突 SHALL 以 `ALIAS_CONFLICT` 拒绝该日提交；未登记的未知代码 SHALL
  仍按 `UNKNOWN_INSTRUMENT` 拒绝；SHALL NOT 按后缀/代码段推断别名；
  SHALL NOT 改写主档数据集。

## Impact

- **代码**：`app/providers/history/tushare_aliases.py`（新增）、
  `app/providers/history/tushare.py`（两条抓取路径接入 + 重新导出）、
  `app/services/history/retry.py`（`ALIAS_CONFLICT` 归入配置类错误）；
- **数据**：无 schema 变更、无迁移。已知旧代码证券的事实数据统一落在规范
  `instrument_id` 下，不新增主档证券；
- **运维**：`ALIAS_CONFLICT` 是新增的终态错误码，会在 `/admin/data` 的
  "最后错误"与"最近执行记录"中出现；管理员需用交易所公告判定权威数据；
- **测试**：`tests/unit/test_history_ts_code_alias.py`（Provider 边界）、
  `tests/integration/test_history_alias_service.py`（Service 端到端）、
  `scripts/spike/verify_ts_code_alias_online.py`（上线前在线只读验证）；
- **不受影响**：既有 `UNKNOWN_INSTRUMENT` 保护、`DUPLICATE_KEY` 校验、
  水位/重试语义、主档刷新路径、API 与页面契约。
