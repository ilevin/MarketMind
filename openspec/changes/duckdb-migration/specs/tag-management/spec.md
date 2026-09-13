## ADDED Requirements

### Requirement: 标签 CRUD API

系统 SHALL 提供 `GET /api/tags`、`POST /api/tags`、`PATCH /api/tags/{tag_id}`、`DELETE /api/tags/{tag_id}`。列表 SHALL 返回 `[{id, name, usage_count}]`，usage_count 为该标签被股票/ETF 自选条目引用的数量（指数不计入）。创建成功返回 201；修改成功返回 200；删除成功返回 204；不存在返回 404。

#### Scenario: 创建标签成功
- **WHEN** POST `/api/tags` `{"name": "高股息"}`
- **THEN** 返回 201，body 含 id 与 name

#### Scenario: 查询标签列表
- **WHEN** GET `/api/tags` 且存在被 5 个自选引用的标签"高股息"
- **THEN** 返回 200，"高股息"条目 `usage_count` 为 5

#### Scenario: 修改标签名称
- **WHEN** PATCH `/api/tags/1` `{"name": "红利策略"}`
- **THEN** 返回 200，标签改名后已有自选关联不受影响（关联按 id 维护）

#### Scenario: 标签不存在
- **WHEN** PATCH 或 DELETE 不存在的 tag_id
- **THEN** 返回 404

### Requirement: 标签命名校验

标签名称 SHALL 去除首尾空格后非空、长度不超过 50 字符、且全库唯一。空名称与超长返回 422；重复名称返回 409。

#### Scenario: 名称去首尾空格
- **WHEN** POST `{"name": " 科技 "}`
- **THEN** 保存名称为"科技"

#### Scenario: 空名称禁止
- **WHEN** POST `{"name": "   "}`
- **THEN** 返回 422，不创建

#### Scenario: 超长名称禁止
- **WHEN** POST 名称去空格后超过 50 字符
- **THEN** 返回 422，不创建

#### Scenario: 重复名称禁止
- **WHEN** 已存在"科技"时 POST `{"name": "科技"}`
- **THEN** 返回 409 Conflict

### Requirement: 标签删除保护

被任一股票/ETF 自选条目引用的标签 SHALL NOT 可删除：返回 409 与包含引用数量的中文错误信息；未被引用的标签可删除（204）。删除标签 SHALL NOT 自动解除自选条目的关联。业务层校验之外，数据库层 SHALL 提供兜底保护：watchlist_tag 对 tag 的外键约束（RESTRICT，无级联）阻止删除被引用的标签。

#### Scenario: 被引用的标签禁止删除
- **WHEN** 标签"高股息"被 5 个自选引用时 DELETE `/api/tags/{id}`
- **THEN** 返回 409，错误信息含引用数量，标签仍存在

#### Scenario: 未被引用的标签可删除
- **WHEN** 标签未被任何自选引用时 DELETE `/api/tags/{id}`
- **THEN** 返回 204，标签从列表消失

#### Scenario: 数据库层兜底保护
- **WHEN** 绕过业务校验直接删除被引用标签（数据库层约束）
- **THEN** DuckDB 外键约束（RESTRICT，无级联）阻止删除，watchlist_tag 关联不产生悬空引用

### Requirement: 标签管理页面

系统 SHALL 提供 `/tags` 页面（Jinja2），支持查看标签列表（名称、使用数量）、新增标签、行内编辑名称、删除标签；删除被引用标签时 SHALL 展示后端 409 错误信息。

#### Scenario: 页面渲染
- **WHEN** 访问 /tags
- **THEN** 显示标签表格（名称/使用数量/操作）与新增表单

#### Scenario: 删除被引用标签的前端反馈
- **WHEN** 在 /tags 页面删除一个被引用的标签
- **THEN** 页面展示后端返回的 409 错误信息，标签仍在列表中

### Requirement: 标签与自选的关联关系

标签与股票/ETF 自选条目 SHALL 为多对多关系：一个标签可关联多个自选条目；一个自选条目也可同时关联多个标签（可空表示无标签）；指数 SHALL NOT 关联标签。标签先创建、后关联，系统 SHALL NOT 支持在自选管理处直接新建标签。

#### Scenario: 删除自选后引用计数减少
- **WHEN** 标签被 2 个自选引用，删除其中 1 个自选
- **THEN** 该标签 usage_count 变为 1
