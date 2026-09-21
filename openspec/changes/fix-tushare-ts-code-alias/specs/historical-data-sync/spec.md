## MODIFIED Requirements

### Requirement: 失败日期绝不跳过

某数据集某交易日处理失败（含校验失败、空结果、截断不可确认）时，SHALL 在单次任务内按退避策略最多重试 10 次；10 次仍失败 SHALL 将该数据集标记 FAILED（记录 failed_trade_date 与最后错误）并停止该数据集本次推进，SHALL NOT 跳到后续日期；下一次定时/手动执行 SHALL 从该失败日期继续。重试退避 SHALL 为 `min(5 × 2^(attempt-1), 300)` 秒并施加 0.8~1.2 随机抖动，全部参数可配置；配置类错误（Token 缺失、权限拒绝、schema 不匹配、别名冲突）SHALL 快速失败并显示明确原因，SHALL NOT 无意义睡满 10 轮。

`ALIAS_CONFLICT`（同一规范证券的新旧 ts_code 在同一交易日给出不一致业务字段）SHALL 归入配置类错误：重复请求上游不会改变结果，必须由人工用权威来源判定哪一套数据正确，因此 SHALL 在首次尝试即判定该交易日失败、写入 `last_error_code=ALIAS_CONFLICT` 与 `failed_trade_date`，水位 SHALL NOT 推进。

#### Scenario: 中间日期失败不越过

- **WHEN** 01-04 成功、01-05 连续 10 次失败
- **THEN** 水位停在 01-04，01-06 从未被请求，run_dataset 记录失败日期与错误

#### Scenario: 下次从失败日恢复

- **WHEN** 上述失败后的下一次运行且 01-05 可成功
- **THEN** 从 01-05 继续，水位最终推进到目标日

#### Scenario: 别名冲突快速失败且不推进水位

- **WHEN** 某交易日 daily 返回 `000022.SZ` 与 `001872.SZ` 两行且业务字段不一致
- **THEN** 该数据集 `last_error_code=ALIAS_CONFLICT`、`status=FAILED`、`failed_trade_date` 为该交易日、`latest_complete_trade_date` 保持不变，且该交易日 SHALL 只请求上游一次（不睡满 10 轮退避）

#### Scenario: 别名冲突不影响其余数据集

- **WHEN** daily 因 `ALIAS_CONFLICT` 失败
- **THEN** adj_factor / daily_basic / moneyflow 三个数据集 SHALL NOT 被阻塞，各自按自身水位继续推进
