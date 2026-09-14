# dashboard-ui Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 行情首页

系统 SHALL 在 `/` 提供首页(要求登录,未登录 302 重定向 `/login`),自上而下为:市场状态区、指数行情卡片区、自选股票/ETF 行情表格(均为当前登录用户的数据)。指数 SHALL NOT 与股票/ETF 混在同一表格。

#### Scenario: 页面结构
- **WHEN** 登录用户访问 /
- **THEN** 依次显示 A股/港股市场状态、指数横向卡片、自选表格

#### Scenario: 未登录重定向
- **WHEN** 未登录访问 /
- **THEN** 302 重定向到 /login

### Requirement: 市场状态展示

首页顶部 SHALL 显示 A股与港股状态（交易中/午间休市/已收盘/休市），用于解释行情是否在自动更新。

#### Scenario: 状态文案
- **WHEN** A股 OPEN 且港股 CLOSED
- **THEN** 显示「A股 · 交易中」「港股 · 已收盘」

### Requirement: 指数卡片

指数区 SHALL 使用横向小卡片，每个指数仅显示名称、当前点位、今日涨跌幅；SHALL NOT 加入 K 线、分时图、走势图。

#### Scenario: 卡片内容
- **WHEN** 显示上证指数
- **THEN** 卡片仅含名称、点位、涨跌幅三要素

### Requirement: 行情表格

表格 SHALL 包含：名称、代码、市场、当前价格、今日涨幅、量比、PE(TTM)、PB、股息率(TTM)、行情更新时间。缺失值 SHALL 显示 `-`；价格数字右对齐；百分比保留两位小数；价格小数位按数据源返回合理展示。港股延时行情 SHALL 在市场列显示「港股 · 延时」。

#### Scenario: 缺失值
- **WHEN** ETF 无 PE 数据
- **THEN** 该单元格显示 `-`（API 层为 null）

#### Scenario: 延时标识
- **WHEN** 港股行情标记 delayed
- **THEN** 市场列显示「港股 · 延时」

### Requirement: 涨跌配色

涨幅 > 0 SHALL 显示红色，< 0 显示绿色，= 0 普通颜色（指数与表格一致）。

#### Scenario: 颜色规则
- **WHEN** 涨跌幅为 +1.25%
- **THEN** 数字为红色

### Requirement: 前端轮询策略

页面首次加载 SHALL 立即读取 /api/quotes 与 /api/indices;市场交易中每 60 秒读取一次;两市场均非 OPEN(午休/收盘/节假日)时 SHALL 停止自动轮询但保留已显示数据。SHALL NOT 整页刷新,SHALL NOT 引入前端框架。所有 fetch 请求 SHALL 统一附带 `X-CSRF-Token` 请求头(由页面 meta 注入)。

#### Scenario: 首次加载
- **WHEN** 收盘后打开首页
- **THEN** 立即读取一次缓存并显示收盘数据,不启动轮询

#### Scenario: 交易中轮询
- **WHEN** 任一市场 OPEN
- **THEN** 每 60 秒读取一次缓存 API 并局部更新

#### Scenario: 空自选
- **WHEN** 当前用户无任何自选标的
- **THEN** 显示「还没有自选标的,去添加一个」提示

#### Scenario: 写请求携带 CSRF 头
- **WHEN** 页面执行任何写操作(添加/删除/排序/标签关联)
- **THEN** fetch 请求携带页面 meta 注入的 X-CSRF-Token

### Requirement: 行情页标签筛选

行情页自选表格区 SHALL 提供标签筛选下拉，选项为「全部」「无标签」与全部已有标签；选择标签后仅展示关联该标签的股票/ETF，选择「无标签」仅展示未打标签条目，默认「全部」。筛选 SHALL 在前端本地完成（基于已加载数据过滤渲染），SHALL NOT 触发任何新的第三方行情请求；60 秒轮询刷新数据后 SHALL 保持当前筛选状态继续生效。

#### Scenario: 按标签筛选
- **WHEN** 选择标签「高股息」
- **THEN** 表格仅显示关联「高股息」的股票/ETF

#### Scenario: 无标签筛选
- **WHEN** 选择「无标签」
- **THEN** 表格仅显示未关联标签的股票/ETF

#### Scenario: 筛选不触发行情请求
- **WHEN** 连续切换筛选选项并观察网络请求与 Provider 调用
- **THEN** 不产生新的行情接口请求与任何 Provider 调用

#### Scenario: 轮询后筛选保持
- **WHEN** 筛选「科技」后 60 秒轮询刷新完成
- **THEN** 表格仍按「科技」过滤展示最新数据

### Requirement: 登录页面

系统 SHALL 提供匿名可访问的 `/login` 页面(详见 user-authentication 规格):用户名/密码表单、错误提示、登录成功跳转首页;`/login`、`/health`、`/static/*` SHALL 为仅有的匿名可访问路由。

#### Scenario: 登录成功跳转
- **WHEN** 在 /login 提交正确凭据
- **THEN** 跳转到 / 并以登录态展示首页

### Requirement: 导航栏用户信息

业务页面导航栏 SHALL 显示当前用户名与角色,提供「修改密码」与「退出登录」入口;管理员额外显示「用户管理」与「系统状态」入口。退出登录 SHALL 调用 logout API 并跳转 /login。

#### Scenario: 普通用户导航栏
- **WHEN** role=user 登录后查看任意业务页面
- **THEN** 导航栏显示用户名、角色,含修改密码与退出登录,不含用户管理入口

#### Scenario: 管理员导航栏
- **WHEN** role=admin 登录后查看任意业务页面
- **THEN** 导航栏额外显示用户管理入口

#### Scenario: 退出登录
- **WHEN** 点击导航栏退出登录
- **THEN** Session 撤销,跳转 /login,回退按钮无法恢复登录态

### Requirement: 修改密码页面

系统 SHALL 提供修改密码界面(旧密码 + 新密码 + 确认新密码),成功后要求重新登录。

#### Scenario: 修改密码成功
- **WHEN** 提交正确旧密码与合规新密码
- **THEN** 提示成功,当前 Session 失效,跳转 /login 重新登录

### Requirement: 管理员用户管理页面

系统 SHALL 提供管理员专属 `/admin/users` 页面(详见 user-management 规格),与既有 Jinja2 页面风格一致,不引入前端框架。

#### Scenario: 管理员使用用户管理页面
- **WHEN** admin 在 /admin/users 创建用户、切换启用状态、重置密码
- **THEN** 各操作即时反馈结果,列表刷新展示最新状态

