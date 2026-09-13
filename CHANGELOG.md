# Changelog

本文件记录 marketmind 的版本演进。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号从 v0.1.0 重新起步（marketmind 是以 stocksview 架构为基础的 DuckDB 演进版，
不继承 stocksview 的 SQLite 版本历史）。

## [v0.1.0] - 2026-09-13

首个版本。以 stocksview（v0.03.1，A 股/港股行情看板）的分层架构与全部产品能力为基础，
持久层从 SQLite 整体切换为 DuckDB，为历史行情存储与回测打地基。

### 新增

- DuckDB 持久层：`duckdb==1.5.5` + `duckdb-sqlalchemy==1.5.5.5`（精确锁定版本），
  数据库文件 `data/marketmind.duckdb`
- 全新 Alembic 基线 `0001_duckdb_baseline`：一次创建全部 10 张核心表与 `seq_tag_id` sequence
- v1 表结构：业务主键取代自增代理 id（`instrument_id` 主键、复合主键、quote_snapshot 一证券一行）、
  显式 sequence、全库 TIMESTAMPTZ aware 时间语义
- `WriteCoordinator` 写事务协调器：进程内锁序列化全部写事务 + 有限重试
  （DuckDB 为嵌入式单写者数据库，乐观并发下同表并发写会冲突）

### 变更

- 部署约束：uvicorn 固定 `--workers 1` 单 worker，后台任务与 Web 请求同进程
- `watchlist_tag` 外键不再使用 `ON DELETE CASCADE`（DuckDB 不支持级联删除），
  删除自选条目时由 Service 在写锁内两段提交先删标签关联、再删条目
  （DuckDB 1.5.5 的 FK 检查看不到同事务内已删的子表行）
- `tag.name` 不设数据库 UNIQUE 约束（DuckDB 1.5.5 中被 FK 引用的父表
  UNIQUE 列不可 UPDATE），重名校验由 TagService 写锁内查重保证，行为不变（409）
- 标签列表计数查询 `GROUP BY` 展开全部列（DuckDB 严格执行 SQL 标准，
  不容忍裸列）
- 自选排序 tie-break 从 `id` 改为 `(sort_order, created_at)`
- `quote_snapshot` 写入由 SELECT-then-UPDATE 改为原子 upsert（主键保证一证券一行）
- 测试体系改跑真实临时 DuckDB 文件（不再用 SQLite 内存库代替），新增共享 conftest

### 破坏性变更

- **旧 stocksview（SQLite 版）部署不能原地升级到本版本**：数据库文件格式不兼容，
  SQLite 历史数据导入工具属后续版本（T14）
- 生产环境不可多 worker / 多进程写同一数据库文件
