## ADDED Requirements

### Requirement: Provider 超时配置

Tencent、AKShare、Tushare 三个 Provider SHALL 各自具备明确的超时配置（`providers.timeout.{tencent,akshare,tushare}`，单位秒，默认 8/15/15），经应用配置注入；配置缺省时 SHALL 使用默认值。Tencent 经 HTTP 客户端参数生效，Tushare 经 SDK 参数生效，AKShare 经调用包装层的限时执行生效。

#### Scenario: 配置指定超时
- **WHEN** config.yaml 配置 `providers.timeout.tencent: 5`
- **THEN** Tencent 行情请求以 5 秒为上限

#### Scenario: 配置缺省使用默认值
- **WHEN** config.yaml 无 providers.timeout 节
- **THEN** 三个 Provider 分别按默认超时运行，应用正常启动

### Requirement: Provider 失败与超时后的行为

Provider 调用失败（无论超时还是报错）SHALL 记为本次调用失败并停止等待（后台刷新任务 SHALL NOT 因单个 Provider 无响应或报错而长期卡住）；失败 SHALL 保留最后一次成功行情（Last Known Good），SHALL NOT 删除已有缓存或快照；下一个刷新周期 SHALL 重新尝试。

#### Scenario: 超时不阻塞刷新周期
- **WHEN** AKShare 全市场请求超过超时时间未返回
- **THEN** 该次调用按失败处理，刷新周期正常结束，旧行情继续展示

#### Scenario: 报错不删除旧行情
- **WHEN** Provider 返回 HTTP 500 或解析失败等非超时错误
- **THEN** 该次调用计入 error，已有行情快照与缓存原样保留

#### Scenario: 失败后下一周期恢复
- **WHEN** 某 Provider 本周期超时或报错，下一周期恢复正常
- **THEN** 下一周期行情正常更新，缓存恢复新鲜

### Requirement: Provider 运行指标

每个 Provider SHALL 维护统一运行指标：request_count、success_count、error_count、timeout_count、last_success_at、last_error_at、last_error、last_duration_ms。error（接口报错/连接失败/解析失败）与 timeout（超过规定时间未完成）SHALL 分开统计。指标 SHALL 由统一封装层（ProviderMetrics）实现，各业务 Provider SHALL NOT 各自实现统计逻辑；指标为进程内存态，重启后重新计数（可接受）。指标变化 SHALL 同步输出结构化日志。

#### Scenario: 正常请求计入成功
- **WHEN** Tencent 请求正常返回
- **THEN** request_count 与 success_count 各增 1，last_duration_ms 更新

#### Scenario: 接口报错计入 error
- **WHEN** Provider 抛出非超时异常（如 HTTP 500、解析失败）
- **THEN** error_count 增 1，last_error_at/last_error 更新

#### Scenario: 超时计入 timeout
- **WHEN** Provider 调用超时
- **THEN** timeout_count 增 1（不计入 error_count），last_error_at 更新

### Requirement: Provider 指标查询接口

`GET /api/admin/status` 的 providers 节 SHALL 按数据源名称返回上述全部指标字段，使开发者可判断哪个 Provider 出问题、最近是否成功、失败多少次、是否超时、最近耗时。

#### Scenario: 查询 Provider 指标
- **WHEN** GET /api/admin/status
- **THEN** providers 含 tencent/akshare/tushare 的 request_count、success_count、error_count、timeout_count、last_duration_ms 等字段
