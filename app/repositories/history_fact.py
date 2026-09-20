"""日级事实表仓储（a-share-historical-data，技术方案 §22/§23、§5.1）。

四张千万行级事实表经 SQLAlchemy Core 批量读写：
- ``count_for_date`` / ``delete_for_date`` / ``insert_records`` 支撑单日原子
  替换事务（§22 步骤 6~8：查 old_count → DELETE 当日 → 批量 INSERT，
  可按 chunk 分批）；
- 不逐行 ORM（§5.1）；批量写入走 DuckDB 注册视图 + INSERT SELECT，
  见 ``insert_records`` 的说明；
- ``max_trade_date`` 仅用于 reconcile 发现矛盾（§25：不能据此自动推进水位）。

事务边界与提交由调用方（HistorySyncService，经 WriteCoordinator）负责。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Iterable

import pandas
from sqlalchemy import Delete, Select, Table, func, select
from sqlalchemy.orm import Session

from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_sync import DatasetName

# 每个 INSERT SELECT 批次的行数（技术方案 §5.1：1000~2000 行 chunk）
INSERT_CHUNK_SIZE = 1000


def _key(dataset: DatasetName | str) -> str:
    """枚举安全转主键字符串（str-mixin Enum 的 str() 含类名，不可直接用）。"""
    return dataset.value if isinstance(dataset, DatasetName) else str(dataset)


def _table(dataset: DatasetName | str) -> Table:
    table = HISTORY_FACT_TABLES.get(_key(dataset))
    if table is None:
        raise ValueError(f"未知的事实数据集: {dataset}（可选: {sorted(HISTORY_FACT_TABLES)}）")
    return table


class HistoryFactRepository:
    """日级事实表 Core 仓储；全部方法在调用方事务内执行。"""

    def __init__(self, session: Session):
        self.session = session

    def count_for_date(self, dataset: DatasetName | str, trade_date: date) -> int:
        """该数据集某交易日的现有行数（§22 步骤 6 的 old_count）。"""
        table = _table(dataset)
        stmt: Select = select(func.count()).select_from(table).where(
            table.c.trade_date == trade_date
        )
        return int(self.session.scalar(stmt) or 0)

    def max_trade_date(self, dataset: DatasetName | str) -> date | None:
        """事实表当前最大交易日（仅 reconcile 发现矛盾用，§25）。"""
        table = _table(dataset)
        stmt: Select = select(func.max(table.c.trade_date))
        return self.session.scalar(stmt)

    def delete_for_date(self, dataset: DatasetName | str, trade_date: date) -> None:
        """整日 DELETE（§23：吸收上游修订、幂等替换）。"""
        table = _table(dataset)
        stmt: Delete = table.delete().where(table.c.trade_date == trade_date)
        self.session.execute(stmt)

    def insert_records(
        self,
        dataset: DatasetName | str,
        records: Iterable,
        *,
        source: str,
        fetched_at: datetime,
        chunk_size: int = INSERT_CHUNK_SIZE,
    ) -> int:
        """批量 INSERT 内部标准模型记录（DuckDB 注册视图 + INSERT SELECT）。

        records 为 ``app.providers.base`` 的冻结 dataclass（字段名与表列一致）；
        source / fetched_at 由本方法注入。返回写入行数。

        实现说明（技术方案 §5.1/§74，design.md Risks 第 5 条）：DuckDB 的
        ``executemany`` 是逐行解析慢路径——实测 6000 行约 2.2~28 s（tmpfs 上
        同样慢，属 CPU 而非磁盘），而把行集注册为 DuckDB 视图后一条
        ``INSERT ... SELECT`` 只需约 0.14 s（同机约 14×）。故此处走"注册视图 +
        INSERT SELECT"（备选 ``duckdb_sqlalchemy.copy_from_rows`` 约 0.54 s，
        慢约 3.8×，且经 CSV 文本中转，详见 design.md 对应风险条目）：

        - 连接取自 ``self.session.connection()``，即当前事务绑定的那条连接，
          **不新开连接**，因此仍在调用方事务内（DELETE/INSERT/day_status/水位
          同属一个事务，可整体回滚）；
        - 类型按 Python 对象直传、不经文本序列化，NULL 与 0、DATE、
          BIGINT/DOUBLE 精度、含逗号引号换行的字符串均无损；
        - 表名/列名是代码内固定常量，仍经方言 preparer 加引号后再拼接。
        """
        table = _table(dataset)
        all_columns = [column.name for column in table.columns]
        column_set = set(all_columns)
        rows: list[dict] = []
        for record in records:
            data = asdict(record)
            row = {name: data[name] for name in data if name in column_set}
            row["source"] = source
            row["fetched_at"] = fetched_at
            rows.append({name: row.get(name) for name in all_columns})
        if not rows:
            return 0

        self._insert_rows_via_staging(table, all_columns, rows, chunk_size)
        return len(rows)

    def _insert_rows_via_staging(
        self,
        table: Table,
        columns: list[str],
        rows: list[dict],
        chunk_size: int,
    ) -> None:
        """把 rows 分批注册为临时视图并 INSERT SELECT 进目标表。

        视图名带随机后缀，避免与同连接上的其他注册对象冲突；每个批次在
        ``finally`` 中注销，异常路径也不残留。
        """
        sa_connection = self.session.connection()
        connection = sa_connection.connection.dbapi_connection
        preparer = sa_connection.dialect.identifier_preparer
        target = preparer.format_table(table)
        column_list = ", ".join(preparer.quote(name) for name in columns)
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            view_name = f"__mm_fact_stage_{uuid.uuid4().hex}"
            connection.register(view_name, pandas.DataFrame(chunk, columns=columns))
            try:
                connection.execute(
                    f"INSERT INTO {target} ({column_list}) "
                    f"SELECT {column_list} FROM {preparer.quote(view_name)}"
                )
            finally:
                connection.unregister(view_name)
