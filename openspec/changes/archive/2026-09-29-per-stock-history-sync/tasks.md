## 1. 前置在线 spike（阻塞调度改造合并，不阻塞离线开发）

- [ ] 1.1 编写 `scripts/spike/verify_stock_range_fetch_online.py`：真实 Token 只读实测四数据集按股票区间拉取（`pro.daily/adj_factor/daily_basic/moneyflow(ts_code=…, start_date=…, end_date=…)`）× 样本股（正常股 16 年全区间、长期停牌股、退市股末段、旧代码股 000022.SZ），记录返回行数、字段完整性、行序、空结果是否无异常返回、单次行数上限行为
- [ ] 1.2 依据 spike 结论回答 design.md Open Questions 1/2（空结果语义、行数上限），把结论固化为离线测试 fake 响应基线并在 design.md 记录结论

## 2. 数据模型与 0004 迁移

- [x] 2.1 `app/models/history_sync.py` 新增 `StockSyncState` ORM（(dataset, instrument_id) 逻辑唯一键；冗余 ts_code、watermark_date、last_task_id、last_status、last_error_code/last_error、last_success_at、last_attempt_at；无 UNIQUE/FK/二级索引）与 `SyncTask` ORM（BIGINT id 经 `seq_sync_task_id` sequence；run_id、dataset、instrument_id、ts_code、start_date/end_date、status、retry_count、attempt_count、records_fetched/records_written、error_code/error_type/error_message、started_at/finished_at/duration_ms）
- [x] 2.2 `HistorySyncRunDataset` 模型增加 `processed_count`/`task_success_count`/`task_failed_count`/`skipped_count`（server_default 0）
- [x] 2.3 创建 `alembic/versions/0004_*.py`（基于当前 head 0003_a_share_historical_data）：建两张新表与 `seq_sync_task_id` sequence、为 `history_sync_run_dataset` 加四列；SHALL NOT 写入任何 stock_sync_state 行
- [x] 2.4 迁移内置只读诊断统计（各数据集事实行数、有数据股票数、最大交易日分布写入迁移日志）与结构校验、幂等重放、downgrade DDL 逆操作
- [x] 2.5 `tests/integration/test_migrations_history.py` 新增 0004 场景（v0.3.1 库升级既有数据无损且新表为空、全新库全链、重复执行幂等、校验失败回滚干净），并更新 `tests/integration/test_migrations.py` 防漂移测试对两张新表、run_dataset 新列与 sequence 的期望

## 3. Provider 区间接口与别名层

- [x] 3.1 `app/providers/base.py` 的 `HistoricalMarketDataProvider` Protocol 新增 `get_history_by_stock(dataset, instrument, start_date, end_date)` 声明（既有按 trade_date 方法与 Protocol 兼容保留）
- [x] 3.2 `TushareHistoricalMarketDataProvider.get_history_by_stock` 实现：四 endpoint 映射、显式 fields 声明、`_ts_code_of_instrument` 构造请求代码（不按代码首位推断）、行数上限 truncation_risk 防护、`raw_row_count` 取别名规范化前口径
- [x] 3.3 `HistoryProviderRegistry` 登记新方法（metrics 键复用 `tushare_history_{dataset}`、超时注入、call_with_metrics 包装）
- [x] 3.4 `app/providers/history/tushare_aliases.py` 并存检测分组键改为 `(canonical_ts_code, trade_date)`（单日输入行为等价），其余语义（仅旧码改写、drop_legacy、ALIAS_CONFLICT、主档不接入）不变
- [x] 3.5 `tests/unit/test_history_provider.py` 新增 get_history_by_stock 单测（FakeTushareClient 模式：区间参数透传断言、多日行、旧代码响应、行数上限、异常分类与 gate 限流、`tushare_history_{dataset}` metrics 计数进入现有体系断言）
- [x] 3.6 `tests/unit/test_history_ts_code_alias.py` 新增区间场景单测（同股多日不误判、区间内某日新旧并存一致去重/冲突抛 ALIAS_CONFLICT、raw_row_count 口径），既有单测全绿

## 4. Repository 与校验层

- [x] 4.1 `app/repositories/history_fact.py` 新增 `delete_for_instrument_range(dataset, instrument_id, start_date, end_date)` 与单股区间替换提交方法（复用 `_insert_rows_via_staging` 批量写与 chunk）
- [x] 4.2 新增 `StockSyncStateRepository`（写锁内 get-or-create、`advance_watermark` 事务内单调不下降校验、按 run 批量补建缺失状态行、last_* 更新、universe 按 watermark 升序 NULLS FIRST + ts_code 升序查询）与 `SyncTaskRepository`（create、finish_success、finish_failed、interrupt_running_for_runs、find_by_id）
- [x] 4.3 `app/services/history/validation.py` 的校验入口 `validate_batch` 增加区间模式（新增 date_range 参数：日期 ∈ [start, end] 且不早于 list_date、不晚于 min(end, delist_date)、批内 (instrument_id, trade_date) 唯一；区间模式 0 行合法，单日模式沿用非零行/EMPTY_RESULT 行为），单日模式与既有测试保留；新增区间校验单测（区间外日期拒绝、早于 list_date 拒绝、批内重复拒绝、同股多日行合法、0 行合法）
- [x] 4.4 `history_sync_state` 的 record_count/data_min_date/data_max_date 维护改为按单股区间替换 new-old 增减（`HistorySyncStateRepository` 相应方法）
- [x] 4.5 `tests/integration/test_history_repositories.py` 扩展：区间替换幂等（同区间重跑无重复行、计数不翻倍）、水位回退拒绝、批量补建唯一性、sync_task 流水不覆盖、universe 处理顺序断言（水位 NULL 先于旧水位先于新水位、同水位按 ts_code 升序）

## 5. 配置与重试语义

- [x] 5.1 `app/config.py` `HistoryConfig` 新增 `max_retries=3`，实现 `max_attempts` 兼容读取（显式配置 max_attempts 且未配置 max_retries 时换算 max_retries = max_attempts - 1 并 WARNING；两者并存以 max_retries 为准），更新 `config.example.yaml` 与说明注释
- [x] 5.2 `app/services/history/retry.py` `RetryPolicy` 改为 max_retries 口径（总尝试 = max_retries + 1），`tests/unit/test_history_retry.py` 更新（换算逻辑、退避序列约 5/10/20 秒 + 0.8~1.2 抖动、配置类错误快速失败不变）

## 6. 执行器与编排重构

- [x] 6.1 新建 `app/services/history/stock_executor.py` `StockSyncExecutor`：单股单数据集任务生命周期（写锁事务创建 sync_task=running → 最多 max_retries+1 次（默认 4）attempt：锁外 fetch → 锁外 validate → 写锁内"区间替换 + 水位推进 + task 终态 + run_dataset 计数"原子提交或失败记录 → 退避 sleep 前后与 attempt 边界检查 cancellation；配置类错误首试即终态；股级日志规范——每股一条概要 INFO（run_id/dataset/ts_code/row_count/elapsed_ms）、重试 WARNING、耗尽 ERROR、错误文本脱敏不含 Token）
- [x] 6.2 `app/services/history/planner.py` 改造：个股有效区间计算（eff_start = max(history.start_date, list_date)；eff_end 上界 = min(target, delist_date)；区间端点收敛到严格交易日历）；移除仅服务旧日级模型的静态函数（pending_dates/lag_days/reconcile_watermark），`tests/unit/test_history_planner.py` 相应改写
- [x] 6.3 `app/services/history/sync_service.py` 重构 `run()`：入口签名、主档前置、single-flight、AvailabilityPolicy 不变；日级数据集改为"解析 target → 批量补建缺失 stock_sync_state 行 → universe 按 (watermark ASC NULLS FIRST, ts_code ASC) → 逐股（无工作 skipped_count+1 不建 task；有工作交 Executor；单股异常记录后 continue；系统级异常终止 Run）"；移除 `_sync_day_level_dataset`/`_sync_single_day`/`_fetch_day`/`_daily_basic_fallback`/`reconcile_daily_watermarks`/`reconcile_dataset` 与 `history_day_status` 写入
- [x] 6.4 `recover_stale_runs` 扩展：把属于已中断 Run 的 running `sync_task` 批量置 interrupted（补 finished_at），绝不推进对应水位
- [x] 6.5 Run 语义与进度：SUCCESS 允许个股 failed、PARTIAL 不再产生；run_dataset 新统计列写入、旧水位列冻结置 NULL/0；进程内 progress 快照（当前 dataset/ts_code、已处理/成功/失败/跳过）经 `app.state` 暴露，聚合计数以 DB 为准
- [x] 6.6 今日成功/失败统计查询（`stock_sync_state.last_attempt_at AT TIME ZONE 'Asia/Shanghai'` = 当天 AND last_status 聚合，仅扫小表），供 summary 与 /stocks 复用

## 7. 同步行为集成测试（重写既有守护）

- [x] 7.1 重写 `tests/integration/test_history_sync_service.py` 为个股口径：个股水位推进与单调不下降、失败隔离（单股重试耗尽失败不影响其他股、Run SUCCESS、task_failed_count=1）、自动补偿（失败股下轮自动从缺口继续并追平）、已追平股票零请求、全部追平时 run.status=NOOP（不写事实、不创建 task）、生命周期边界（中途上市从 list_date 起、退市股同步至 delist_date、delist < start 的退市股 skipped 不建任务）、系统级异常场景（注入数据库错误 → Run FAILED、未提交股票水位不动、已成功股票进度保留）
- [x] 7.2 空结果语义集成测试（停牌区间 0 行无异常 → 推进水位 records_fetched=0、退市末段/moneyflow 非覆盖合法为空、请求异常进入重试路径）
- [x] 7.3 别名与未知代码集成测试（旧代码股区间回填落规范码并推进水位；ALIAS_CONFLICT 只失败该股不阻塞数据集；UNKNOWN_INSTRUMENT 拒绝不建占位证券）
- [x] 7.4 中断恢复集成测试（进程中断后 running task → interrupted、水位不动、下轮按原水位重同步；优雅停机完成当前股事务后停止；`test_history_sync_job_lifespan.py` 相应更新）
- [x] 7.5 幂等与重复运行集成测试（同区间重跑事实行数不变、record_count 不翻倍、任务流水新增而水位不变）
- [x] 7.6 今日统计多时区单测（UTC 服务器时区下北京时间跨日归属正确；同日先失败后成功只计成功）

## 8. 管理 API 与前端

- [x] 8.1 `app/schemas/history_admin.py` 与 `app/api/admin_history.py`：summary 升级个股口径（stock_count/up_to_date_count/lagging_count/today_success_count/today_failed_count/completion_rate；旧水位字段保留输出冻结值；overall_status 按 RUNNING→ERROR(系统级)→LAGGING(有个股缺口)→HEALTHY），顺手修复 requested_by_user_id int→str 类型瑕疵
- [x] 8.2 新增 `GET /api/admin/history-data/stocks`（dataset 必填仅四日级数据集、status=all|success|failed、q 名称/代码 LIKE、服务端分页固定 100、默认排序 last_status='failed' 优先 → watermark ASC NULLS FIRST → ts_code ASC、响应含统计块与分页元信息，SQL 内 JOIN 完成）
- [x] 8.3 新增 `GET /api/admin/history-data/tasks/{task_id}`（按 id 直查 + JOIN 主档补名称、404、只读）
- [x] 8.4 `app/templates/admin_data.html` 数据集卡片改个股口径（含今日成功/失败、完整度）、当前任务进度改（数据集/股票/已处理/成功/失败/跳过）、新增"个股历史"导航入口；最近 20 次执行记录的各数据集统计改用新列（processed/success/failed/skipped）展示，`/runs` 与 `/runs/{run_id}` 响应补充新统计列与运行中实时进度（旧水位列为冻结兼容输出）
- [x] 8.5 新增 `app/templates/admin_data_stocks.html` 与 `app/static/app.js` 个股页逻辑（数据集切换 chip、状态筛选、搜索、100 条分页控件"上一页/下一页/共 N 条"、失败行只读详情 modal 经 esc() 转义、复用现有 badge/table/modal 样式与 api() 工具、运行中轮询沿用）
- [x] 8.6 `tests/integration/test_admin_history_api.py` 与 `tests/integration/test_admin_data_page.py` 更新扩展（summary 新字段口径、overall_status 分支断言——个股失败 → LAGGING 非 ERROR、全追平 → HEALTHY、active run → RUNNING、系统级失败 → ERROR、/stocks 分页筛选排序与 422、/tasks 详情与 404、/runs/{run_id} 新统计列与旧水位兼容字段断言、权限矩阵 403/401 与 CSRF、个股页渲染与导航）

## 9. 性能基准与全量回归

- [x] 9.1 `scripts/bench/bench_history_write.py` 增加个股区间场景（单股约 4000 行 × 多股连续提交），断言每股提交语句数与事务数不随股数/行数逐行放大
- [x] 9.2 全量离线回归：`.venv/bin/python -m pytest -m "not online" -q` 全绿（含受影响既有测试的改写：migrations、admin API/页面、sync service、job lifespan、repositories、retry、planner）
- [x] 9.3 性能基准文档化（`docs/testing/v0.4.0-performance-benchmark.md`：两场景结果、加速比分析、回归阈值建议、生产环境预估）

## 10. 在线冒烟、文档与发布

- [ ] 10.1 `tests/integration/test_history_online_smoke.py` 扩展：四数据集按股票区间真实请求断言（含 spike 样本股的空结果与行数上限语义），沿用 `@pytest.mark.online` 只读约定
- [x] 10.2 更新 `docs/CHANGELOG.md`（v0.4.0 条目：个股水位模型、Run/Task 两层、失败隔离与自动补偿、API/页面变化、升级注意）与 `docs/README.md` 历史数据章节（个股口径说明、升级后首轮全量回填预期约数小时跨多轮完成、数据总览完成率从 0% 逐股上升属正常）
- [x] 10.3 `config.example.yaml` history 节 max_retries 注释已完整（max_attempts 标记 deprecated）；部署文档确认停服升级、`.duckdb`+`.wal` 成对备份、回滚=恢复备份（README.md 已更新）
- [ ] 10.4 `app/version.py` 版本号升至 v0.4.0；最终校验（全量 pytest、代码检查）并提交
