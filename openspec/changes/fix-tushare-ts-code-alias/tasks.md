## 1. Phase 0：现状确认（只读）

- [x] 1.1 定位所有相关实现与测试：`app/providers/history/tushare.py`（`_symbol_map` / `_instrument_for_ts_code` / `_fetch_day_level` / `_fetch_day_level_per_instrument`）、`app/services/history/sync_service.py`（`_daily_basic_fallback`、`_sync_single_day` 的 UNKNOWN_INSTRUMENT 恢复路径）、`app/services/history/validation.py`（`_check_common` 的 `known_instrument_ids` 保护与 `DUPLICATE_KEY`）、`app/services/history/retry.py`（`CONFIG_ERROR_CODES`）、`tests/unit/test_history_provider.py`、`tests/integration/test_history_sync_service.py`。确认两条日级抓取路径是仅有的 df 入口，别名规范化放在它们的 df 边界即可覆盖四个数据集。
- [x] 1.2 确认插入点：`_check_dataframe` 之后、`_symbol_map` 之前；`raw_rows = len(df)` 保持规范化前取值。确认主档三个数据集（stock_basic / stock_company / namechange）不接入。

## 2. Phase 1：先写失败测试

- [x] 2.1 `tests/unit/test_history_ts_code_alias.py`：规范化纯函数口径（不修改输入、缺 ts_code 列抛 SCHEMA_MISMATCH、空 DataFrame 直通、仅旧代码改写、旧+新一致只留规范行、冲突抛 ALIAS_CONFLICT、非别名重复保持原样交给 DUPLICATE_KEY）。
- [x] 2.2 同文件：冲突比较容差（None/NaN 等价、`1` 与 `1.0` 等价、`-`/`""` 等价、`10.0` vs `10.01` 仍冲突、日期不同冲突）。
- [x] 2.3 同文件：四数据集 × 主路径参数化（§13 Test 1/2/3/4/5/7）+ 四数据集 × fallback 参数化（§11.2）。
- [x] 2.4 同文件：不退化断言——`DUPLICATE_KEY` 仍拦截普通重复、`known_instrument_ids` 保护仍生效、`ALIAS_CONFLICT` 属配置类错误、`stock_basic` 不被别名化。
- [x] 2.5 运行新测试确认失败（`ImportError: cannot import name 'HistoricalAliasConflictError'`）。

## 3. Phase 2：实现

- [x] 3.1 新增 `app/providers/history/tushare_aliases.py`：`TUSHARE_TS_CODE_ALIASES`（首条 `000022.SZ -> 001872.SZ`）、`canonical_ts_code()`、`HistoricalAliasConflictError`（`error_code=ALIAS_CONFLICT`，继承 `TushareError` 以免与 `tushare.py` 形成循环依赖）、`_values_equal` / `_rows_equal` / `_describe_differences`、`normalize_historical_aliases()`。
- [x] 3.2 `app/providers/history/tushare.py`：导入并重新导出别名层符号（`__all__`），在 `_fetch_day_level` 与 `_fetch_day_level_per_instrument` 的 `_check_dataframe` 之后插入规范化调用。
- [x] 3.3 `app/services/history/retry.py`：`CONFIG_ERROR_CODES` 增加 `ALIAS_CONFLICT`。

## 4. Phase 3：集成测试

- [x] 4.1 `tests/integration/test_history_alias_service.py`：真实 `TushareHistoricalMarketDataProvider` + 真实临时 DuckDB + fake SDK client，覆盖四个日级数据集水位推进、落库 ts_code 为规范代码、无重复事实键、不创建 `CN:STOCK:000022`。
- [x] 4.2 同文件：`ALIAS_CONFLICT` 快速失败（只请求一次）、`run_dataset` 记录错误码与失败日、不影响其余数据集推进。
- [x] 4.3 同文件：`daily_basic` 候选集差额基于规范代码（`001872.SZ` 不被误判为缺失）、主路径批次单证券单记录且 `raw_row_count` 保留原始规模、fallback 逐只请求按规范代码发起。

## 5. Phase 4：在线验证脚本

- [x] 5.1 `scripts/spike/verify_ts_code_alias_online.py`：只读诊断，扫描 `stock_basic` 全部 15 分片确认旧代码是否真的不在主档，并对 2010-01-04 / 2010-01-05 在四个 endpoint 上打印「只返回旧代码 / 只返回新代码 / 新旧都返回」与逐字段比较结论；输出不含 Token（只打印是否配置），不写任何数据库。

## 6. Phase 5：发布前审计与加固（提交前）

- [x] 6.1 四视角审计（凭证泄漏 / 别名逻辑正确性 / 文档一致性 / 测试有效性）+ 对抗性证伪。
- [x] 6.2 修复审计确认的缺陷：别名链合并且检测冲突（D10）、numpy 标量空值判定、超范围整数不泄漏裸 `OverflowError`；配套回归测试并做变异验证（禁用修复即失败）。
- [x] 6.3 修正恒真用例 `test_candidate_missing_diff_is_computed_on_canonical_codes`：压低 `DAILY_ROW_CAP` 真实驱动 `Service._daily_basic_fallback`，并断言截断确已发生（禁用别名层即失败）。
- [x] 6.4 在线验证脚本加固：规范代码不在主档时给出「停止部署」advisory。
- [x] 6.5 记录 Known Limitations：主档同时含新旧两码时 `daily_basic` 补齐撞 `DUPLICATE_KEY`（根因是候选集与 Provider 输出身份口径不一致，需 Service 层改动）。

## 7. Phase 6：回归与发布

- [x] 7.1 相关测试：`tests/unit/test_history_ts_code_alias.py`（55）+ `tests/integration/test_history_alias_service.py`（11）+ `test_history_provider.py` + `test_history_retry.py` + `test_history_sync_service.py` 共 166 passed。
- [x] 7.2 全量离线 `pytest`：**629 passed / 10 deselected**（549.69 s，无 failed 无 error；基线 563 passed，新增 66）。
- [ ] 7.3 上线前在线验证（需真实 Token，由运维执行）：`scripts/spike/verify_ts_code_alias_online.py`，据此确认别名表策略。三条停止线：①"新旧并存且冲突"；②规范代码不在主档；③旧代码仍出现在 `stock_basic` 分片中（见 Known Limitations）。
- [ ] 7.4 构建并部署修复镜像（不覆盖 v0.3.0 标签），备份生产库与配置后启动，在 `/admin/data` 观察前几个交易日确认水位越过 2010-01-04。
