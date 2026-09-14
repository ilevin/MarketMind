# tag-management Specification

## Purpose
TBD - created by archiving change duckdb-migration. Update Purpose after archive.
## Requirements
### Requirement: 标签 CRUD API

系统 SHALL 提供 `GET /api/tags`、`POST /api/tags`、`PATCH /api/tags/{tag_id}`、`DELETE /api/tags/{tag_id}`,全部要求登录且仅作用于当前登录用户的标签;访问不属于当前用户的 tag_id 返回 404。列表 SHALL 返回 `[{id, name, usage_count}]`,usage_count 为该标签被当前用户股票/ETF 自选条目引用的数量(指数不计入)。创建成功返回 201;修改成功返回 200;删除成功返回 204;不存在(或不属于当前用户)返回 404。

#### Scenario: 创建标签成功
- **WHEN** POST `/api/tags` `{"name": "高股息"}`
- **THEN** 返回 201,body 含 id 与 name

#### Scenario: 查询标签列表
- **WHEN** GET `/api/tags` 且当前用户存在被 5 个自选引用的标签"高股息"
- **THEN** 返回 200,"高股息"条目 `usage_count` 为 5

#### Scenario: 修改标签名称
- **WHEN** PATCH `/api/tags/1` `{"name": "红利策略"}`
- **THEN** 返回 200,标签改名后已有自选关联不受影响(关联按 id 维护)

#### Scenario: 标签不存在
- **WHEN** PATCH 或 DELETE 不存在(或不属于当前用户)的 tag_id
- **THEN** 返回 404

### Requirement: 标签命名校验

标签名称 SHALL 去除首尾空格后非空、长度不超过 50 字符、且在同一用户的标签集合内唯一(全库唯一改为用户内唯一)。空名称与超长返回 422;同一用户内重复名称返回 409;不同用户创建同名标签 SHALL 成功。

#### Scenario: 名称去首尾空格
- **WHEN** POST `{"name": " 科技 "}`
- **THEN** 保存名称为"科技"

#### Scenario: 空名称禁止
- **WHEN** POST `{"name": "   "}`
- **THEN** 返回 422,不创建

#### Scenario: 超长名称禁止
- **WHEN** POST 名称去空格后超过 50 字符
- **THEN** 返回 422,不创建

#### Scenario: 重复名称禁止
- **WHEN** 当前用户已存在"科技"时 POST `{"name": "科技"}`
- **THEN** 返回 409 Conflict

#### Scenario: 跨用户同名允许
- **WHEN** 用户 A 已有"科技"标签,用户 B POST `{"name": "科技"}`
- **THEN** 返回 201,B 拥有自己的"科技"标签

### Requirement: 标签删除保护

被当前用户任一股票/ETF 自选条目引用的标签 SHALL NOT 可删除:返回 409 与包含引用数量的中文错误信息;未被引用的标签可删除(204)。删除标签 SHALL NOT 自动解除自选条目的关联。业务层校验之外,数据库层 SHALL 提供兜底保护:watchlist_tag 对 tag 的外键约束(RESTRICT,无级联)阻止删除被引用的标签。

#### Scenario: 被引用的标签禁止删除
- **WHEN** 标签"高股息"被当前用户 5 个自选引用时 DELETE `/api/tags/{id}`
- **THEN** 返回 409,错误信息含引用数量,标签仍存在

#### Scenario: 未被引用的标签可删除
- **WHEN** 标签未被任何自选引用时 DELETE `/api/tags/{id}`
- **THEN** 返回 204,标签从列表消失

#### Scenario: 数据库层兜底保护
- **WHEN** 绕过业务校验直接删除被引用标签(数据库层约束)
- **THEN** DuckDB 外键约束(RESTRICT,无级联)阻止删除,watchlist_tag 关联不产生悬空引用

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

### Requirement: 标签命名空间用户隔离

`tag` 表 SHALL 增加 `user_id` 外键关联 `app_user.user_id`,标签名唯一范围 SHALL 为同一 `user_id` 内唯一;`watchlist_tag` SHALL 以 `(user_id, instrument_id, tag_id)` 为复合主键并外键关联 `watchlist(user_id, instrument_id)`。用户 A SHALL NOT 能读取、修改、删除用户 B 的标签,也不能将 B 的 tag_id 绑定到自己的自选(返回 404)。标签管理页面 `/tags` SHALL 要求登录,仅展示与操作当前用户的标签。

#### Scenario: 标签列表仅见自己的
- **WHEN** 用户 A 与 B 各有若干标签,A 请求 GET /api/tags
- **THEN** 仅返回 A 的标签

#### Scenario: 删除他人标签返回 404
- **WHEN** 用户 A DELETE 用户 B 的 tag_id
- **THEN** 返回 404,B 的标签不受影响

#### Scenario: 关联计数按用户计算
- **WHEN** 用户 A 与 B 都有"科技"标签且各自关联若干自选
- **THEN** 各自 GET /api/tags 返回的 usage_count 仅统计本用户自选的引用

