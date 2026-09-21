## Context

`HistorySyncService` 的水位按"严格交易日"逐个推进，单日任一数据集失败即
停止该数据集本轮推进、水位保持在上一成功交易日（§22/§70.2）。因此一个
**永久性**的映射失败会把回填整体卡死——不是"跳过这一天继续"，而是从此
无法前进。

线上首次回填实测（v0.3.0，`000022.SZ`）：

```text
daily        FAILED  error_code=UNKNOWN_INSTRUMENT  failed_trade_date=2010-01-04
adj_factor   FAILED  error_code=UNKNOWN_INSTRUMENT  failed_trade_date=2010-01-04
daily_basic  FAILED  error_code=UNKNOWN_INSTRUMENT  failed_trade_date=2010-01-04
moneyflow    FAILED  error_code=UNKNOWN_INSTRUMENT  failed_trade_date=2010-01-05
```

现状代码（`app/providers/history/tushare.py`）：

```python
symbol_map = {inst.symbol: inst for inst in _cn_stock_instruments(instruments)}

def _instrument_for_ts_code(ts_code, symbol_map):
    inst = symbol_map.get(_symbol_of(ts_code))
    if inst is None:
        raise UnknownInstrumentError(f"ts_code 无法映射至证券主档: {ts_code}")
    return inst
```

`stock_basic` 已取全部 3 交易所 × 5 上市状态共 15 个分片，旧代码在其中
任何分片都不存在——§35.1 的"刷新主档一次后重映射"恢复路径对本类问题
结构上无效（刷新不会让上游补回一个已废弃的代码）。

## Goals / Non-Goals

**Goals:**

- 已登记的证券代码变更不再阻塞历史回填；
- 同一只证券的历史事实全部落在**同一个** `instrument_id` 下，不产生第二
  条证券、不产生重复事实键；
- 上游对同一证券同一天给出互相矛盾的数据时**显式失败**，不静默择一；
- 严格保护不退化：未登记的未知代码仍 `UNKNOWN_INSTRUMENT`，主档校验、
  `DUPLICATE_KEY`、水位与重试语义全部不变；
- 四个日级数据集与两条抓取路径行为一致；
- 无数据库迁移。

**Non-Goals:**

- 不做通用的 ts_code 历史代码自动发现（不按后缀、代码段、名称相似度推断）；
- 不改 `stock_basic` / `stock_company` / `namechange`（主档是 instrument 的
  来源，改写会造出重复证券）；
- 不做跨数据库/跨表的存量数据回填或修复（修复只影响后续抓取）；
- 不为 `stock_company` 多出的记录自动建档（§9.5），与本变更无关；
- 不引入配置项控制别名表（登记表是代码常量，随代码评审变更）。

## Decisions

### D1：别名层放在 Provider 边界，不放在 Repository / Service / UI

理由：身份映射本就是 Provider 的职责（§31.3、Providers §"ts_code 到
instrument_id 映射"），且 Tushare DataFrame 不越过 Provider 边界。放在
Service 会要求 Service 理解 Tushare 的 ts_code 语义；放在 Repository 会
让事实表与主档表看到两套身份口径。

### D2：独立模块 `tushare_aliases.py`，而非塞进 `tushare.py`

`tushare.py` 已 700+ 行且职责密集（字段常量、行构建器、两条抓取路径）。
别名规则表会长期增长（每次证券代码变更追加一条），独立模块使"规则 + 比较
策略 + 错误码"自成一体，便于单独测试与评审。

依赖方向：`tushare.py` → `tushare_aliases.py`（单向）。
`HistoricalAliasConflictError` 继承 `TushareError`（在
`tushare_common.py`，更底层），不继承 `TushareHistorySchemaError`——后者
在 `tushare.py` 中定义，继承它会形成循环依赖。`normalize_historical_aliases`
内部按需导入 `TushareHistorySchemaError` 用于"缺 ts_code 列"这一种结构错误。

### D3：插入点在 `_check_dataframe` 之后、`_symbol_map` 之前

```python
df = self._transport.call(...)
_check_dataframe(df, context=context)
raw_rows = len(df)                                  # ← 规范化前取值
df = normalize_historical_aliases(df, endpoint=endpoint, trade_date=trade_date)
symbol_map = _symbol_map(instruments)                # ← 映射看到的是规范代码
records = _normalize_rows(...)
```

`raw_row_count` 保持上游真实返回规模：合并掉一行时 `raw_row_count` 仍为
2，`len(records)` 为 1——监控不会被规范化"美化"。

### D4：两条抓取路径共用同一 helper

`_fetch_day_level_per_instrument`（截断 fallback）逐只请求用的是证券
**今天的**代码，但上游对历史日期仍可能回旧代码。只修主路径会让 fallback
在补齐时重新出现身份不一致，因此两条路径调用同一个函数。

### D5：冲突比较不用 `DataFrame.equals`

`equals` 会被 NaN/None 语义、dtype（`1` vs `1.0`）、字符串与数值表示、
字段顺序制造假冲突。改用：

1. 排除 `ts_code`（已被改写，逐行比较无意义）；
2. 空值按 `_cell` 同口径归一（`None` / `NaN` / `NaT` / `""` / `-` / `--`）；
3. 数值走**相对**容差 `1e-9`（相对量级取 `max(1.0, |a|, |b|)`），吸收浮点
   序列化尾差与 `1`/`1.0` 差异，但 `10.0` vs `10.01` 仍然冲突；
4. 日期统一为 YYYYMMDD 整数（`date`/`datetime`/`"20100104"` 可比）；
5. 任何"无法证明相等"的情况一律判为**不等**（保守失败）。

### D6：情形 C 失败而非择一

新旧并存且字段冲突说明上游对同一证券同一交易日给了两套互相矛盾的事实。
此时无论选哪一套都可能污染历史序列，且无法从数据本身判定权威性。选择
`ALIAS_CONFLICT` + 不推进水位，由人工用交易所公告判定。**这是有意的
"宁可阻塞不可污染"**——与 §12 "宁可阻止水位，也不要静默吞掉不一致数据"
一致。

诊断信息只含 endpoint、trade_date、两个 ts_code 与不一致字段名/取值
（Tushare 事实数据不含任何凭据），异常文本经既有 `_safe_error_text`
脱敏后落库。

### D7：`ALIAS_CONFLICT` 归入配置类错误

`CONFIG_ERROR_CODES` 的语义是"不会随重试自愈、需人工介入"（§29.3）。
别名冲突正是这一类：重复请求上游只会得到同样的两套数据。归入配置类错误
使其一次尝试即终态失败，而不是白等 10 轮指数退避（最长 300 s/轮）。

### D8：不改主档数据集

`stock_basic` 的 `ts_code` 是 `cn_stock_basic` 主键与 instrument 来源；
`stock_company` / `namechange` 同样以主档身份为键。改写它们会凭空造出
第二条证券记录或触发主键冲突。别名层只作用于四个日级事实数据集。

### D9：别名表是逐条登记的常量，不是规则

每条登记都必须能回答"这是同一法人主体的代码变更"。禁止按 `.SZ/.SH/.BJ`
后缀或代码段自动推断——那会把互不相关的证券静默合并到同一身份下。
登记表的正确性由代码评审 + `scripts/spike/verify_ts_code_alias_online.py`
的在线只读验证共同保证。

### D10：两步别名链在合并时按"组内是否含字面规范行"分两种参考行选取

同一规范代码出现多行时，合并需要一个参考行来比较业务字段：

- 组内**有**字面规范代码行（`A` + `C`，`C` 是规范代码）→ 以该行为参考，
  其余行与之比较；
- 组内**没有**字面规范行、但来自**不同**旧代码（`A` + `B`，两者都登记
  指向 `C`）→ 以首行为参考，其余行与之比较。这是两步别名链的形态：不
  处理会退化成 `DUPLICATE_KEY`——既不检测真正的冲突，又要白等重试；
- 组内全是**同一个**旧代码 → 原样保留，交给既有 `DUPLICATE_KEY`（本层
  不承担通用去重职责）。

同代码的多行重复（无论哪种形态）都不在本层去重：`DUPLICATE_KEY` 是
Domain 校验的职责，削弱它会让真实的重复数据被静默吞掉（§9.2）。

## Known Limitations

**别名改写不能跨身份消重（本次不修，需 Service 层改动）。**

`daily_basic` 截断补齐的候选集来自 `cn_stock_basic`（`list_ts_codes_tradable_on`），
而 Provider 输出的 `ts_code` 会被别名层改写成规范代码。若主档**同时**含新旧
两码（例如 `stock_basic` 的 D/UN 分片仍返回旧代码，使 `cn_stock_basic` 里
既有 `000022.SZ` 又有 `001872.SZ`）：

- 候选集含 `000022.SZ`；主路径规范化后 `returned` 只有 `001872.SZ`；
- `missing = {000022.SZ}`，逐只请求旧代码 → 上游回旧代码 → 规范化成
  `001872.SZ` → 与主路径已产出的记录撞成 `DUPLICATE_KEY`；
- （若回退到逐只全量 fallback，两条记录同样撞成 `DUPLICATE_KEY`。）

两种形态都卡住水位。根因是**候选集与 Provider 输出用了两套身份口径**，
正确修法是让 Service 在构造候选集/差额时统一到规范代码（或让 Provider
在候选集一侧做同样的别名映射）——那是 Service 层职责，与 D1"身份映射留在
Provider 边界"冲突，需要单独设计。

**触发前提**：`000022.SZ` 出现在 `stock_basic` 分片中。线上实测（本修复
的起因）是主档只有 `001872.SZ`，此时旧代码不进候选集，上述路径不可达。
`scripts/spike/verify_ts_code_alias_online.py` 第 1 节专门检查这一点——
**若该脚本报告旧代码仍在主档分片中，不要部署**，先按上面的方向评估 Service
层改动。

## Risks / Trade-offs

| 风险 | 说明 | 处置 |
| --- | --- | --- |
| 登记表本身写错 | 把两只不同证券登记为同一只，历史数据被静默合并 | 逐条评审 + 在线验证脚本核对主档；表极小（当前 1 条） |
| 上游行为与 2019 年不同 | 计划明确警告"不要假设 2019 年 issue 描述的返回行为在 2026 年仍逐字段完全相同" | 上线前必须跑 `verify_ts_code_alias_online.py`；冲突情形已有确定性失败路径 |
| `drop_legacy` 掩盖真实数据差异 | 两行"看起来一致"但存在容差内差异被丢弃 | 容差为相对 1e-9，只吸收浮点尾差；WARNING 日志带两端代码与日期可追溯 |
| 静默放行所有未知代码 | 用户可能希望"修好了就别再报错" | 未登记代码仍 `UNKNOWN_INSTRUMENT`，有单测锁定（`test_unregistered_unknown_code_still_rejected`） |
| 新错误码未在 UI 说明 | `/admin/data` 只展示 `last_error_code` 与文本 | 错误文本自带中文说明（含两个代码与冲突字段），页面已按现状渲染 |
| 主档同时含新旧两码 | 详见 Known Limitations：候选集与 Provider 输出身份口径不一致，`daily_basic` 补齐会撞 `DUPLICATE_KEY` | 上线前用在线验证脚本第 1 节确认旧代码**不在**主档；若在，先评估 Service 层改动再部署 |

## Migration Plan

无数据库迁移。代码上线后：

1. 先跑 `scripts/spike/verify_ts_code_alias_online.py`（只读，不碰数据库），
   确认 `000022.SZ` 在四个 endpoint 上的真实返回形态；
2. 若结论是"只返回旧代码"或"新旧并存且一致"，直接构建新镜像部署；
3. 若结论是"新旧并存且冲突"，**先不要部署**——需人工判定权威数据；
4. 部署后先在 `/admin/data` 观察前几个交易日，确认水位越过 2010-01-04。

回滚：镜像回退到修复前版本即可（无 schema 变更、无数据格式变更）。
