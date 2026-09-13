## ADDED Requirements

### Requirement: 统一配置文件

应用配置 SHALL 统一保存在 config.yaml（database、quote.refresh_seconds/stale_seconds、tushare.token、providers、logging），由 app/config.py 启动时读取并注入；database.url 取值形如 `duckdb:///./data/marketmind.duckdb`（config.example.yaml 示例与 DatabaseConfig.url 默认值同步）；业务代码 SHALL NOT 直接读取 YAML。默认刷新周期 SHALL 为 60 秒，stale 阈值 180 秒。

#### Scenario: 配置读取
- **WHEN** 应用启动
- **THEN** 各 Provider/Service 收到统一配置对象，刷新周期为 60 秒

### Requirement: Token 安全

config.yaml 含真实 Tushare Token 时 SHALL 列入 .gitignore 不提交；仓库 SHALL 只提交不含真实 Token 的 config.example.yaml；Token SHALL NOT 写入数据库；日志 SHALL NOT 输出 Token 或完整敏感配置；SHALL NOT 依赖 TUSHARE_TOKEN 环境变量或 .env。

#### Scenario: Token 读取
- **WHEN** config.yaml 配置了 tushare.token
- **THEN** Tushare Provider 从配置对象获得 Token

#### Scenario: 日志脱敏
- **WHEN** 记录配置错误日志
- **THEN** 日志不包含 Token 明文

#### Scenario: 缺少 Token
- **WHEN** 未配置 Token 启动
- **THEN** 应用可启动，Tushare 功能记录明确配置错误，其他功能正常

### Requirement: 日志规范

系统 SHALL 使用标准 logging，至少记录：应用启动、数据库初始化、行情刷新开始/完成、AKShare/Tushare 请求失败、单个证券解析失败、自选增删、后台任务异常。

#### Scenario: 刷新日志
- **WHEN** 一轮行情刷新完成
- **THEN** 日志记录开始与完成（含成功/失败统计）
