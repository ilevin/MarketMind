## ADDED Requirements

### Requirement: 行情首页

系统 SHALL 在 `/` 提供首页，自上而下为：市场状态区、指数行情卡片区、自选股票/ETF 行情表格。指数 SHALL NOT 与股票/ETF 混在同一表格。

#### Scenario: 页面结构
- **WHEN** 访问 /
- **THEN** 依次显示 A股/港股市场状态、指数横向卡片、自选表格

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

页面首次加载 SHALL 立即读取 /api/quotes 与 /api/indices；市场交易中每 60 秒读取一次；两市场均非 OPEN（午休/收盘/节假日）时 SHALL 停止自动轮询但保留已显示数据。SHALL NOT 整页刷新，SHALL NOT 引入前端框架。

#### Scenario: 首次加载
- **WHEN** 收盘后打开首页
- **THEN** 立即读取一次缓存并显示收盘数据，不启动轮询

#### Scenario: 交易中轮询
- **WHEN** 任一市场 OPEN
- **THEN** 每 60 秒读取一次缓存 API 并局部更新

#### Scenario: 空自选
- **WHEN** 无任何自选标的
- **THEN** 显示「还没有自选标的，去添加一个」提示

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
