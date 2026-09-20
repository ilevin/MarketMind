"""历史数据 Repository/Validator 集成测试（a-share-historical-data，tasks 4.5）。

真实临时 DuckDB（conftest session fixture）+ mock 标准模型记录：
- 批量写（注册 DuckDB 视图 + INSERT SELECT 分 chunk）与 source/fetched_at 注入；
- 整日替换（DELETE 当日 + 重插）与主档 upsert / namechange 替换语义；
- 校验拒绝路径（§35~§39 各错误码）与"拒绝即不落库"的组合行为。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.models.history_market import CnStockBasic, CnStockCompany, CnStockNameChange
from app.models.history_sync import (
    DatasetKind,
    DatasetName,
    DatasetStatus,
    HistorySyncRun,
    RunDatasetStatus,
    RunStatus,
    TriggerType,
)
from app.models.instrument import Instrument
from app.providers.base import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    MoneyFlow,
    ProviderBatch,
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)
from app.models.history_fact import HISTORY_FACT_TABLES
from app.repositories.history_fact import HistoryFactRepository
from app.repositories.history_master import HistoryMasterRepository, namechange_event_key
from app.repositories.history_sync import (
    HistoryDayStatusRepository,
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
)
from app.services.history import validation as val

FETCHED_AT = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
RUN_ID = "run-test-0001"


# ---- 记录构造 helper ----


def basic_record(symbol: str, **overrides) -> StockBasicRecord:
    fields = dict(
        ts_code=f"{symbol}.SZ",
        symbol=symbol,
        instrument_id=f"CN:STOCK:{symbol}",
        name=f"股票{symbol}",
        area="深圳",
        industry="银行",
        exchange="SZSE",
        list_status="L",
        list_date=date(1991, 4, 3),
    )
    fields.update(overrides)
    return StockBasicRecord(**fields)


def daily_record(symbol: str, trade_date: date, **overrides) -> DailyBar:
    fields = dict(
        instrument_id=f"CN:STOCK:{symbol}",
        ts_code=f"{symbol}.SZ",
        trade_date=trade_date,
        open=10.0,
        high=11.0,
        low=9.5,
        close=10.5,
        vol=12345.0,
        amount=23456.0,
    )
    fields.update(overrides)
    return DailyBar(**fields)


def namechange_record(symbol: str, name: str, start: date, **overrides) -> StockNameChangeRecord:
    fields = dict(
        ts_code=f"{symbol}.SZ",
        instrument_id=f"CN:STOCK:{symbol}",
        name=name,
        start_date=start,
        end_date=None,
        ann_date=None,
        change_reason="更名",
    )
    fields.update(overrides)
    return StockNameChangeRecord(**fields)


def batch(records: list, *, raw: int | None = None, truncation: bool = False) -> ProviderBatch:
    return ProviderBatch(
        records=records,
        source="tushare",
        raw_row_count=raw if raw is not None else len(records),
        truncation_risk=truncation,
    )


# ---- HistoryFactRepository：批量写与整日替换 ----


class TestHistoryFactRepository:
    def test_insert_chunked_count_and_meta(self, session):
        repo = HistoryFactRepository(session)
        trade_date = date(2026, 9, 16)
        records = [
            daily_record(f"{60000 + i:06d}", trade_date, close=10.0 + i / 100)
            for i in range(2500)
        ]
        written = repo.insert_records("daily", records, source="tushare", fetched_at=FETCHED_AT)
        assert written == 2500
        assert repo.count_for_date("daily", trade_date) == 2500

        from app.models.history_fact import HISTORY_FACT_TABLES

        table = HISTORY_FACT_TABLES["daily"]
        sample = session.execute(
            select(table.c.close, table.c.source).where(
                table.c.instrument_id == "CN:STOCK:060000"
            )
        ).one()
        assert sample.close == pytest.approx(10.0)
        assert sample.source == "tushare"

    def test_whole_day_replace_semantics(self, session):
        """§22/§23：old_count → DELETE 当日 → 重插，吸收上游修订。"""
        repo = HistoryFactRepository(session)
        d1, d2 = date(2026, 9, 15), date(2026, 9, 16)
        repo.insert_records(
            "daily",
            [daily_record("000001", d1), daily_record("000002", d1), daily_record("000001", d2)],
            source="tushare",
            fetched_at=FETCHED_AT,
        )
        assert repo.count_for_date("daily", d1) == 2
        assert repo.max_trade_date("daily") == d2

        # d1 整日替换为修订版（000002 涨停价修订）
        repo.delete_for_date("daily", d1)
        assert repo.count_for_date("daily", d1) == 0
        assert repo.count_for_date("daily", d2) == 1, "其他日期不受影响"
        repo.insert_records(
            "daily",
            [daily_record("000001", d1), daily_record("000002", d1, high=11.0)],
            source="tushare",
            fetched_at=FETCHED_AT,
        )
        assert repo.count_for_date("daily", d1) == 2

        from app.models.history_fact import HISTORY_FACT_TABLES

        table = HISTORY_FACT_TABLES["daily"]
        high = session.execute(
            select(table.c.high).where(
                table.c.instrument_id == "CN:STOCK:000002", table.c.trade_date == d1
            )
        ).scalar_one()
        assert high == 11.0

    def test_unknown_dataset_raises(self, session):
        repo = HistoryFactRepository(session)
        with pytest.raises(ValueError, match="未知的事实数据集"):
            repo.count_for_date("stock_basic", date(2026, 9, 16))


# ---- 批量写入实现（注册视图 + INSERT SELECT）的事务与类型回归 ----


class TestBatchInsertTransactionSemantics:
    """回归：批量写入内部实现的改变不得动摇 §22 的单日原子性。

    实现从 ``executemany`` 换成"注册 DuckDB 视图 + INSERT SELECT"（详见
    ``HistoryFactRepository.insert_records``）。该实现取自
    ``session.connection().connection.dbapi_connection``，即当前事务绑定的
    同一连接——本类逐条钉死这一前提以及类型保真。
    """

    def test_uses_same_connection_not_a_separate_one(self, session_factory):
        """必须是当前事务的连接：另一连接看不到未提交数据。"""
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.insert_records(
                "daily",
                [daily_record("000001", date(2026, 9, 16))],
                source="tushare",
                fetched_at=FETCHED_AT,
            )
            # 同事务可见
            assert repo.count_for_date("daily", date(2026, 9, 16)) == 1
            # 事务外不可见（证明没有自动提交、也没另开连接写库）
            with session_factory() as other:
                assert HistoryFactRepository(other).count_for_date(
                    "daily", date(2026, 9, 16)
                ) == 0
            session.rollback()

    def test_rollback_after_insert_discards_rows(self, session_factory):
        """insert 后回滚：整批消失。"""
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.delete_for_date("daily", date(2026, 9, 16))
            repo.insert_records(
                "daily",
                [daily_record(f"{i:06d}", date(2026, 9, 16)) for i in range(200)],
                source="tushare",
                fetched_at=FETCHED_AT,
            )
            session.rollback()
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date(
                "daily", date(2026, 9, 16)
            ) == 0

    def test_rollback_after_insert_restores_prior_day(self, session_factory):
        """insert 后回滚：当日旧数据完整保留（不是"删了旧的又没写新的"）。"""
        day = date(2026, 9, 16)
        with session_factory() as session:
            HistoryFactRepository(session).insert_records(
                "daily",
                [daily_record("000001", day, close=1.0), daily_record("000002", day, close=2.0)],
                source="tushare",
                fetched_at=FETCHED_AT,
            )
            session.commit()
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.delete_for_date("daily", day)                    # 整日替换开始
            repo.insert_records(
                "daily", [daily_record("000001", day, close=9.9)],
                source="tushare", fetched_at=FETCHED_AT,
            )
            session.rollback()                                    # 中途失败
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            assert repo.count_for_date("daily", day) == 2, "旧数据须完整保留"
            table = HISTORY_FACT_TABLES["daily"]
            closes = {
                row.instrument_id: row.close
                for row in session.execute(select(table).where(table.c.trade_date == day)).all()
            }
            assert closes["CN:STOCK:000001"] == pytest.approx(1.0), "未被新值污染"

    def test_exception_after_delete_before_insert_rolls_back(self, session_factory):
        """DELETE 之后异常（insert 从未执行）：旧数据仍在。"""
        day = date(2026, 9, 16)
        with session_factory() as session:
            HistoryFactRepository(session).insert_records(
                "daily", [daily_record("000001", day)], source="tushare",
                fetched_at=FETCHED_AT,
            )
            session.commit()
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.delete_for_date("daily", day)
            session.rollback()      # 模拟 DELETE 后、insert 前的异常
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date("daily", day) == 1

    def test_exception_after_insert_before_watermark_rolls_back_everything(
        self, session_factory
    ):
        """insert 成功但水位更新前异常：事实行与水位一并回滚（§22 核心保证）。"""
        day = date(2026, 9, 16)
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            state_repo = HistorySyncStateRepository(session)
            state_repo.ensure(
                DatasetName.DAILY,
                dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
                history_start_date=date(2026, 9, 10),
            )
            session.commit()
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            state_repo = HistorySyncStateRepository(session)
            repo.insert_records(
                "daily", [daily_record(f"{i:06d}", day) for i in range(300)],
                source="tushare", fetched_at=FETCHED_AT,
            )
            state_repo.complete_day(
                DatasetName.DAILY, day, rows_delta=300, status=DatasetStatus.CAUGHT_UP
            )
            session.rollback()      # 模拟提交前异常
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date("daily", day) == 0
            state = HistorySyncStateRepository(session).get(DatasetName.DAILY)
            assert state.latest_complete_trade_date is None, "水位不得单独前进"

    def test_repeated_execution_is_idempotent(self, session_factory):
        """§23：同一日重复执行（DELETE + 重插）不产生重复行。"""
        day = date(2026, 9, 16)
        records = [daily_record(f"{i:06d}", day, close=5.0) for i in range(500)]
        for _ in range(3):
            with session_factory() as session:
                repo = HistoryFactRepository(session)
                repo.delete_for_date("daily", day)
                repo.insert_records(
                    "daily", records, source="tushare", fetched_at=FETCHED_AT
                )
                session.commit()
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date("daily", day) == 500

    def test_6000_rows_multi_chunk(self, session_factory):
        """6000 行跨多个 chunk（INSERT_CHUNK_SIZE=1000）一次事务写入。"""
        day = date(2026, 9, 16)
        records = [daily_record(f"{i:06d}", day, close=3.0 + i) for i in range(6000)]
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            written = repo.insert_records(
                "daily", records, source="tushare", fetched_at=FETCHED_AT
            )
            assert written == 6000
            assert repo.count_for_date("daily", day) == 6000
            session.commit()
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date("daily", day) == 6000

    def test_empty_records_writes_nothing(self, session_factory):
        """空列表：不注册视图、不报错、返回 0。"""
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            assert repo.insert_records(
                "daily", [], source="tushare", fetched_at=FETCHED_AT
            ) == 0
            assert repo.count_for_date("daily", date(2026, 9, 16)) == 0

    def test_all_four_fact_tables_roundtrip(self, session_factory):
        """四张事实表列数不同（16/6/22/23），逐表验证列映射与类型。"""
        day = date(2026, 9, 16)
        cases = {
            "daily": [daily_record("000001", day)],
            "adj_factor": [
                AdjFactor(
                    instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                    trade_date=day, adj_factor=1.2345,
                )
            ],
            "daily_basic": [
                DailyBasic(
                    instrument_id="CN:STOCK:000001", ts_code="000001.SZ", trade_date=day,
                    turnover_rate=1.5, volume_ratio=0.9, pe=12.3, pb=1.1,
                    total_share=100.0, float_share=80.0, free_share=60.0,
                    total_mv=1234.5, circ_mv=987.6, limit_status=1,
                )
            ],
            "moneyflow": [
                MoneyFlow(
                    instrument_id="CN:STOCK:000001", ts_code="000001.SZ", trade_date=day,
                    buy_sm_vol=1.0, buy_sm_amount=2.0, sell_sm_vol=3.0, sell_sm_amount=4.0,
                    net_mf_vol=-5.0, net_mf_amount=-6.0,
                )
            ],
        }
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            for dataset, records in cases.items():
                assert repo.insert_records(
                    dataset, records, source="tushare", fetched_at=FETCHED_AT
                ) == 1
            session.commit()
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            for dataset in cases:
                assert repo.count_for_date(dataset, day) == 1, f"{dataset} 未写入"
            # 抽查列映射没串位：pe/limit_status 落在预期的列上
            table = HISTORY_FACT_TABLES["daily_basic"]
            row = session.execute(
                select(table.c.pe, table.c.limit_status).where(table.c.trade_date == day)
            ).one()
            assert row.pe == pytest.approx(12.3)
            assert row.limit_status == 1

    def test_type_fidelity_null_zero_and_precision(self, session_factory):
        """类型保真：NULL≠0、DOUBLE 精度、DATE、含特殊字符的字符串。"""
        day = date(2026, 9, 16)
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.insert_records(
                "daily",
                [
                    # 可空列为 NULL（ah_* 允许 NULL）
                    daily_record("000001", day, ah_vol=None, ah_amount=None),
                    # 显式 0 不得被当成 NULL
                    daily_record("000002", day, ah_vol=0.0, ah_amount=0.0),
                    # 高精度 DOUBLE
                    daily_record("000003", day, close=1.2345678901234567),
                    # 负数
                    daily_record("000004", day, change=-9.99, ah_amount=-1.5),
                    # 全字段非 NULL（对照行：证明上面的 NULL 是"值本身为 NULL"，
                    # 不是整列被实现吞成 NULL）
                    daily_record("000005", day, ah_vol=7.0, ah_amount=8.0),
                ],
                source='src,with"quote\nand unicode 数据源',
                fetched_at=FETCHED_AT,
            )
            session.commit()
        with session_factory() as session:
            table = HISTORY_FACT_TABLES["daily"]
            rows = {
                r.instrument_id: r
                for r in session.execute(
                    select(table).where(table.c.trade_date == day)
                ).all()
            }
        assert rows["CN:STOCK:000001"].ah_vol is None
        assert rows["CN:STOCK:000002"].ah_vol == 0.0, "0 不得丢成 NULL"
        assert rows["CN:STOCK:000003"].close == 1.2345678901234567, "精度不得被截断"
        assert rows["CN:STOCK:000004"].change == pytest.approx(-9.99)
        assert rows["CN:STOCK:000004"].ah_amount == pytest.approx(-1.5)
        assert rows["CN:STOCK:000005"].ah_vol == pytest.approx(7.0)
        assert rows["CN:STOCK:000005"].ah_amount == pytest.approx(8.0)
        for row in rows.values():
            assert row.trade_date == day, "DATE 列不得错位"
            assert row.source == 'src,with"quote\nand unicode 数据源'
            assert row.instrument_id.startswith("CN:STOCK:")

    def test_no_staging_view_leaks_after_insert(self, session_factory):
        """异常与正常路径都不得在连接上残留注册视图。"""
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.insert_records(
                "daily", [daily_record("000001", date(2026, 9, 16))],
                source="tushare", fetched_at=FETCHED_AT,
            )
            raw = session.connection().connection.dbapi_connection
            views = raw.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name LIKE '__mm_fact_stage%'"
            ).fetchall()
            assert views == [], f"注册视图泄漏: {views}"
            session.rollback()

    def test_invalid_row_aborts_whole_batch(self, session_factory):
        """整批失败即整体回滚，不留下"写了一半"的当日数据。"""
        day = date(2026, 9, 16)
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.insert_records(
                "daily",
                [daily_record(f"{i:06d}", day) for i in range(100)],
                source="tushare",
                fetched_at=FETCHED_AT,
            )
            session.commit()
        with session_factory() as session:
            repo = HistoryFactRepository(session)
            repo.delete_for_date("daily", day)
            # trade_date 为 None 会触发 NOT NULL 约束失败
            with pytest.raises(Exception):
                repo.insert_records(
                    "daily",
                    [daily_record("000001", day), daily_record("000002", None)],
                    source="tushare", fetched_at=FETCHED_AT,
                )
            session.rollback()
        with session_factory() as session:
            assert HistoryFactRepository(session).count_for_date("daily", day) == 100, (
                "失败事务不得破坏当日既有数据"
            )


# ---- HistoryMasterRepository：upsert 与 namechange 替换 ----


class TestHistoryMasterRepository:
    def test_stock_basic_upsert_creates_and_updates(self, session):
        repo = HistoryMasterRepository(session)
        repo.upsert_stock_basic(
            [basic_record("000001"), basic_record("600519", exchange="SSE", ts_code="600519.SH")],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )

        instruments = {
            i.instrument_id: i for i in session.scalars(select(Instrument)).all()
        }
        assert set(instruments) == {"CN:STOCK:000001", "CN:STOCK:600519"}
        assert instruments["CN:STOCK:000001"].is_active is True
        assert instruments["CN:STOCK:000001"].exchange == "SZSE"
        basic = session.get(CnStockBasic, "CN:STOCK:000001")
        assert basic.name == "股票000001"
        assert basic.sync_run_id == RUN_ID

        # 二次 upsert：改名 + 退市（list_status='D'）——不删除，仅 is_active=false
        repo.upsert_stock_basic(
            [
                basic_record("000001", name="新名字", list_status="D", delist_date=date(2026, 8, 1)),
            ],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        assert session.get(CnStockBasic, "CN:STOCK:000001") is not None, "退市不删除主档"
        inst = session.get(Instrument, "CN:STOCK:000001")
        assert inst.is_active is False
        assert inst.name == "新名字"

    def test_stock_basic_none_keeps_old_values(self, session):
        """上游 None 字段不破坏已有数据（§7.1）。"""
        repo = HistoryMasterRepository(session)
        repo.upsert_stock_basic(
            [basic_record("000001", exchange="SZSE", name="原名")],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        repo.upsert_stock_basic(
            [basic_record("000001", exchange=None, name=None, list_status=None)],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        inst = session.get(Instrument, "CN:STOCK:000001")
        assert inst.exchange == "SZSE"
        assert inst.name == "原名"
        assert inst.is_active is True

    def test_stock_company_upsert(self, session):
        repo = HistoryMasterRepository(session)
        repo.upsert_stock_basic(
            [basic_record("000001")], source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        record = StockCompanyRecord(
            ts_code="000001.SZ",
            instrument_id="CN:STOCK:000001",
            com_name="平安银行股份有限公司",
            chairman="董事长",
            employees=12345,
        )
        assert repo.upsert_stock_company(
            [record], source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT
        ) == 1
        repo.upsert_stock_company(
            [StockCompanyRecord(ts_code="000001.SZ", instrument_id="CN:STOCK:000001", com_name="修订名称")],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        assert repo.count_stock_company() == 1  # upsert 不重复
        assert repo.count_stock_basic() == 1

    def test_stock_company_skips_codes_absent_from_master(self, session):
        """主档中不存在的证券跳过写入，不触发 instrument 外键失败。

        回归（真实数据发现）：Tushare stock_company 覆盖面比 stock_basic 宽
        （实测 6294 vs 5915），多出的代码无法映射到 instrument。直接 upsert
        会外键约束失败，把非阻塞的公司资料刷新变成整轮报错。
        """
        repo = HistoryMasterRepository(session)
        repo.upsert_stock_basic(
            [basic_record("000001")], source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        written = repo.upsert_stock_company(
            [
                StockCompanyRecord(
                    ts_code="000001.SZ", instrument_id="CN:STOCK:000001", com_name="平安银行"
                ),
                StockCompanyRecord(
                    ts_code="000991.SZ", instrument_id="CN:STOCK:000991", com_name="孤立条目"
                ),
            ],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        assert written == 1, "只写入主档中存在的证券"
        assert repo.count_stock_company() == 1
        assert session.get(CnStockCompany, "CN:STOCK:000991") is None

    def test_namechange_replace_for_instrument(self, session):
        """整证券替换（§10.2）：删除全部旧事件后插入当前返回集。"""
        repo = HistoryMasterRepository(session)
        iid = "CN:STOCK:000001"
        repo._insert_name_changes(
            [
                namechange_record("000001", "深发展", date(1991, 1, 1)),
                namechange_record("000001", "平安银行", date(2012, 8, 27)),
            ],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        # 替换为最新返回集
        n = repo.replace_name_changes_for_instrument(
            iid,
            [namechange_record("000001", "深发展", date(1991, 1, 1)),
             namechange_record("000001", "平安银行", date(2012, 8, 27))],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        assert n == 2
        assert repo.count_name_changes() == 2
        # 空返回集：该证券无改名历史是合法结果（同样清空）
        n = repo.replace_name_changes_for_instrument(
            iid, [], source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        assert n == 0
        assert repo.count_name_changes() == 0

    def test_namechange_replace_in_window_keeps_earlier(self, session):
        """重叠窗口增量（§10.3）：只删窗口内（start_date >= window_start），保留更早历史。"""
        repo = HistoryMasterRepository(session)
        window_start = date(2020, 1, 1)
        repo._insert_name_changes(
            [
                namechange_record("000001", "深发展", date(1991, 1, 1)),
                namechange_record("000001", "旧窗口内名", date(2021, 6, 1)),
            ],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        repo.replace_name_changes_in_window(
            [namechange_record("000001", "新窗口内名", date(2021, 6, 1))],
            window_start=window_start,
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        names = session.scalars(select(CnStockNameChange.name)).all()
        assert set(names) == {"深发展", "新窗口内名"}

    def test_namechange_event_key_stable(self):
        key1 = namechange_event_key("000001.SZ", "平安银行", date(2012, 8, 27))
        assert key1 == namechange_event_key("000001.SZ", "平安银行", date(2012, 8, 27))
        assert key1 != namechange_event_key("000001.SZ", "平安银行", None)
        assert key1 != namechange_event_key("000002.SZ", "平安银行", date(2012, 8, 27))
        assert len(key1) == 64

    def test_list_cn_stock_instruments_includes_delisted(self, session):
        repo = HistoryMasterRepository(session)
        repo.upsert_stock_basic(
            [
                basic_record("000002"),
                basic_record("000001", list_status="D"),
            ],
            source="tushare",
            run_id=RUN_ID,
            fetched_at=FETCHED_AT,
        )
        listed = repo.list_cn_stock_instruments()
        assert [i.symbol for i in listed] == ["000001", "000002"], "按 symbol 排序且含退市"


# ---- 控制表四仓储 ----


class TestHistorySyncStateRepository:
    def test_ensure_idempotent(self, session):
        repo = HistorySyncStateRepository(session)
        state = repo.ensure("daily", dataset_kind=DatasetKind.DAILY_CONTIGUOUS, history_start_date=date(2005, 1, 4))
        assert state.dataset == "daily"
        assert state.status == DatasetStatus.UNINITIALIZED.value
        again = repo.ensure(DatasetName.DAILY, dataset_kind=DatasetKind.DAILY_CONTIGUOUS)
        assert again is state  # 幂等不重复建

    def test_complete_day_progress(self, session):
        repo = HistorySyncStateRepository(session)
        repo.ensure("daily", dataset_kind=DatasetKind.DAILY_CONTIGUOUS)
        repo.complete_day("daily", date(2026, 9, 15), rows_delta=5000, status=DatasetStatus.SYNCING)
        repo.complete_day("daily", date(2026, 9, 16), rows_delta=5100, status=DatasetStatus.SYNCING)
        state = repo.get("daily")
        assert state.latest_complete_trade_date == date(2026, 9, 16)
        assert state.current_trade_date is None
        assert state.current_attempt == 0
        assert state.record_count == 10100
        assert state.data_min_date == date(2026, 9, 15)
        assert state.data_max_date == date(2026, 9, 16)

    def test_attempt_error_success_master(self, session):
        repo = HistorySyncStateRepository(session)
        repo.ensure("stock_basic", dataset_kind=DatasetKind.MASTER)
        repo.mark_started("stock_basic", status=DatasetStatus.SYNCING)
        repo.begin_attempt("daily", date(2026, 9, 16), 2, status=DatasetStatus.RETRYING)
        repo.finish_error(
            "daily", error_code="TUSHARE_RATE_LIMIT", error="每分钟最多访问该接口5次"
        )
        # daily 未 ensure → finish_error 目标行不存在也不报错（update 0 行，Service 责任先行 ensure）
        state = repo.get("daily")
        assert state is None

        repo.ensure("daily", dataset_kind=DatasetKind.DAILY_CONTIGUOUS)
        repo.begin_attempt("daily", date(2026, 9, 16), 1, status=DatasetStatus.RETRYING)
        state = repo.get("daily")
        assert state.current_trade_date == date(2026, 9, 16)
        assert state.current_attempt == 1
        assert state.status == DatasetStatus.RETRYING.value

        repo.finish_error("daily", error_code="EMPTY_RESULT", error="返回 0 行")
        state = repo.get("daily")
        assert state.status == DatasetStatus.FAILED.value
        assert state.last_error_code == "EMPTY_RESULT"
        assert state.current_trade_date == date(2026, 9, 16), "失败保留当前日供展示"

        repo.finish_success("daily", status=DatasetStatus.CAUGHT_UP)
        repo.update_master(
            "stock_basic",
            record_count=5000,
            data_min_date=date(1991, 1, 1),
            bootstrap_complete=True,
        )
        master = repo.get("stock_basic")
        assert master.record_count == 5000
        assert master.bootstrap_complete is True
        assert repo.get("daily").status == DatasetStatus.CAUGHT_UP.value
        assert repo.get("daily").last_success_at is not None

    def test_set_expected(self, session):
        repo = HistorySyncStateRepository(session)
        repo.ensure("daily", dataset_kind=DatasetKind.DAILY_CONTIGUOUS)
        repo.set_expected("daily", date(2026, 9, 16))
        assert repo.get("daily").latest_expected_trade_date == date(2026, 9, 16)


class TestHistoryDayStatusRepository:
    def test_upsert_overwrite_and_queries(self, session):
        repo = HistoryDayStatusRepository(session)
        repo.upsert_complete(
            "daily", date(2026, 9, 15), row_count=5000, run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        repo.upsert_complete(
            "daily", date(2026, 9, 16), row_count=5100, run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        assert repo.has_complete("daily", date(2026, 9, 16)) is True
        assert repo.has_complete("daily", date(2026, 9, 17)) is False

        # 重跑同日覆盖（整日替换语义）
        repo.upsert_complete(
            "daily", date(2026, 9, 16), row_count=5150, run_id="run-2", fetched_at=FETCHED_AT
        )
        assert repo.completed_dates("daily") == {date(2026, 9, 15), date(2026, 9, 16)}
        assert repo.completed_dates("daily", start=date(2026, 9, 16)) == {date(2026, 9, 16)}
        assert repo.completed_dates("daily", end=date(2026, 9, 15)) == {date(2026, 9, 15)}
        # 其他数据集互不可见
        assert repo.completed_dates("adj_factor") == set()


class TestHistorySyncRunRepositories:
    def test_run_lifecycle_and_stale(self, session):
        runs = HistorySyncRunRepository(session)
        started = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
        runs.create(
            RUN_ID,
            trigger_type=TriggerType.SCHEDULED,
            requested_by_user_id=None,
            started_at=started,
        )
        assert runs.get(RUN_ID).status == RunStatus.RUNNING.value
        assert [r.run_id for r in runs.find_stale_running()] == [RUN_ID]

        runs.finish(
            RUN_ID, status=RunStatus.SUCCESS, finished_at=started.replace(hour=7)
        )
        assert runs.find_stale_running() == []
        assert runs.get(RUN_ID).finished_at is not None
        assert [r.run_id for r in runs.list_recent()] == [RUN_ID]

    def test_mark_interrupted(self, session):
        runs = HistorySyncRunRepository(session)
        started = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
        runs.create(
            "run-stale", trigger_type=TriggerType.SCHEDULED, requested_by_user_id="u1",
            started_at=started,
        )
        runs.mark_interrupted("run-stale", finished_at=started.replace(hour=8))
        run = runs.get("run-stale")
        assert run.status == RunStatus.INTERRUPTED.value
        assert run.requested_by_user_id == "u1"

    def test_run_dataset_counts_increment(self, session):
        runs = HistorySyncRunRepository(session)
        rds = HistorySyncRunDatasetRepository(session)
        started = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
        runs.create(
            RUN_ID, trigger_type=TriggerType.MANUAL, requested_by_user_id="u1", started_at=started
        )
        rds.start(
            RUN_ID,
            "daily",
            start_watermark=date(2005, 1, 4),
            target_trade_date=date(2026, 9, 16),
            started_at=started,
        )
        # §22 步骤 11：单日事务内增量累计
        rds.add_counts(RUN_ID, "daily", dates=1, rows=5000, requests=1)
        rds.add_counts(RUN_ID, "daily", dates=1, rows=5100, requests=1, retries=1)
        row = rds.get(RUN_ID, DatasetName.DAILY)
        assert row.dates_completed == 2
        assert row.rows_written == 10100
        assert row.request_count == 2
        assert row.retry_count == 1

        rds.finish(
            RUN_ID,
            "daily",
            status=RunDatasetStatus.SUCCESS,
            finished_at=started.replace(hour=7),
            end_watermark=date(2026, 9, 16),
        )
        assert rds.get(RUN_ID, "daily").status == RunDatasetStatus.SUCCESS.value
        assert len(rds.list_for_run(RUN_ID)) == 1


# ---- Validator：拒绝路径与端到端组合 ----


class TestValidationRejectPaths:
    D = date(2026, 9, 16)
    KNOWN = {"CN:STOCK:000001"}

    def _validate(self, records, *, truncation=False, trade_date=D, known=KNOWN, allow_empty=False, raw=None):
        val.validate_batch(
            "daily",
            batch(records, truncation=truncation, raw=raw),
            trade_date=trade_date,
            known_instrument_ids=known,
            allow_empty=allow_empty,
        )

    def test_empty_result(self):
        with pytest.raises(val.EmptyResultError):
            self._validate([])

    def test_empty_result_allowed_when_flagged(self):
        self._validate([], allow_empty=True, raw=0)  # WAITING_SOURCE 由 Service 判定

    def test_truncation_risk(self):
        with pytest.raises(val.TruncationRiskError):
            self._validate([daily_record("000001", self.D)], truncation=True)

    def test_duplicate_key(self):
        with pytest.raises(val.DuplicateKeyError):
            self._validate([daily_record("000001", self.D), daily_record("000001", self.D)])

    def test_trade_date_mismatch(self):
        with pytest.raises(val.TradeDateMismatchError):
            self._validate([daily_record("000001", date(2026, 9, 15))])

    def test_unknown_instrument(self):
        with pytest.raises(val.HistoryUnknownInstrumentError):
            self._validate([daily_record("999999", self.D)])

    @pytest.mark.parametrize(
        "overrides",
        [
            {"vol": -1.0},  # §36 成交量非负
            {"amount": -0.1},
            {"open": -5.0},
            {"high": 9.0},  # high 低于 OHLC 其余值
            {"low": 10.8},  # low 高于 open/close
            {"high": float("nan")},
        ],
    )
    def test_daily_invalid_values(self, overrides):
        with pytest.raises(val.InvalidValueError):
            self._validate([daily_record("000001", self.D, **overrides)])

    def test_daily_ah_null_ok_and_negative_rejected(self):
        self._validate([daily_record("000001", self.D, ah_vol=None, ah_amount=None)])
        with pytest.raises(val.InvalidValueError):
            self._validate([daily_record("000001", self.D, ah_vol=-100.0)])

    def test_adj_factor_rules(self):
        good = batch([AdjFactor(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                                trade_date=self.D, adj_factor=105.3)])
        val.validate_batch("adj_factor", good, trade_date=self.D, known_instrument_ids=self.KNOWN)
        with pytest.raises(val.InvalidValueError):
            val.validate_batch(
                "adj_factor",
                batch([AdjFactor(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                                 trade_date=self.D, adj_factor=0.0)]),
                trade_date=self.D,
                known_instrument_ids=self.KNOWN,
            )
        with pytest.raises(val.DuplicateKeyError):
            twice = [AdjFactor(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                               trade_date=self.D, adj_factor=1.0)] * 2
            val.validate_batch("adj_factor", batch(twice), trade_date=self.D,
                               known_instrument_ids=self.KNOWN)

    def test_daily_basic_negative_share_rejected_pe_null_ok(self):
        from app.providers.base import DailyBasic

        good = DailyBasic(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                          trade_date=self.D, pe=None, total_share=1.0, total_mv=2.0)
        val.validate_batch("daily_basic", batch([good]), trade_date=self.D,
                           known_instrument_ids=self.KNOWN)
        bad = DailyBasic(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                         trade_date=self.D, float_share=-1.0)
        with pytest.raises(val.InvalidValueError):
            val.validate_batch("daily_basic", batch([bad]), trade_date=self.D,
                               known_instrument_ids=self.KNOWN)

    def test_daily_basic_limit_status_enum_range(self):
        """§38：limit_status 允许 NULL，或在 Tushare 定义的 0~6 范围内。"""
        from app.providers.base import DailyBasic

        def _rec(**overrides):
            return DailyBasic(
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                trade_date=self.D, **overrides,
            )

        # NULL 与边界值均合法
        for value in (None, 0, 6):
            val.validate_batch(
                "daily_basic", batch([_rec(limit_status=value)]),
                trade_date=self.D, known_instrument_ids=self.KNOWN,
            )
        # 越界：几乎只可能是字段串位/解析错误，必须拦截
        for value in (7, -1, 99):
            with pytest.raises(val.InvalidValueError, match="limit_status"):
                val.validate_batch(
                    "daily_basic", batch([_rec(limit_status=value)]),
                    trade_date=self.D, known_instrument_ids=self.KNOWN,
                )

    def test_moneyflow_negative_buy_rejected_net_negative_ok(self):
        good = MoneyFlow(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                         trade_date=self.D, buy_sm_vol=100, net_mf_amount=-50.5)
        val.validate_batch("moneyflow", batch([good]), trade_date=self.D,
                           known_instrument_ids=self.KNOWN)
        bad = MoneyFlow(instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                        trade_date=self.D, sell_lg_amount=-1.0)
        with pytest.raises(val.InvalidValueError):
            val.validate_batch("moneyflow", batch([bad]), trade_date=self.D,
                               known_instrument_ids=self.KNOWN)

    def test_stock_basic_mapping_consistency(self):
        good = batch([basic_record("000001")])
        val.validate_batch("stock_basic", good)
        with pytest.raises(val.InvalidValueError, match="映射不一致"):
            mismatch = StockBasicRecord(
                ts_code="000001.SZ", symbol="000001", instrument_id="CN:STOCK:999999"
            )
            val.validate_batch("stock_basic", batch([mismatch]))

    def test_namechange_duplicate_event(self):
        records = [namechange_record("000001", "平安银行", date(2012, 8, 27))] * 2
        with pytest.raises(val.DuplicateKeyError):
            val.validate_batch(
                "namechange", batch(records), known_instrument_ids=self.KNOWN
            )

    def test_unknown_dataset_raises(self):
        with pytest.raises(ValueError, match="无校验规则"):
            val.validate_batch("nope", batch([daily_record("000001", self.D)]))


class TestValidateThenPersistEndToEnd:
    """主档 → 校验 → 落库 → 账本，与"校验拒绝即不落库"（§22）。"""

    def test_master_then_daily_then_day_status(self, session):
        master = HistoryMasterRepository(session)
        facts = HistoryFactRepository(session)
        day_status = HistoryDayStatusRepository(session)

        # 1) 主档先行
        master.upsert_stock_basic(
            [basic_record("000001"), basic_record("000002")],
            source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT,
        )
        known = {i.instrument_id for i in master.list_cn_stock_instruments()}
        assert known == {"CN:STOCK:000001", "CN:STOCK:000002"}

        # 2) 校验通过 → 整日原子替换（§22 步骤 6~8 的事务内序列）
        trade_date = date(2026, 9, 16)
        records = [daily_record("000001", trade_date), daily_record("000002", trade_date, high=12.0)]
        val.validate_batch("daily", batch(records), trade_date=trade_date, known_instrument_ids=known)
        old_count = facts.count_for_date("daily", trade_date)
        facts.delete_for_date("daily", trade_date)
        written = facts.insert_records(
            "daily", records, source="tushare", fetched_at=FETCHED_AT
        )
        day_status.upsert_complete(
            "daily", trade_date, row_count=written, run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        assert old_count == 0 and written == 2
        assert facts.count_for_date("daily", trade_date) == 2
        assert day_status.has_complete("daily", trade_date)

    def test_rejected_batch_writes_nothing(self, session):
        master = HistoryMasterRepository(session)
        facts = HistoryFactRepository(session)
        master.upsert_stock_basic(
            [basic_record("000001")], source="tushare", run_id=RUN_ID, fetched_at=FETCHED_AT
        )
        known = {i.instrument_id for i in master.list_cn_stock_instruments()}
        trade_date = date(2026, 9, 16)

        # 坏批次（high 低于 OHLC 其余值）在写入前被拒绝
        bad_records = [daily_record("000001", trade_date, high=0.1)]
        with pytest.raises(val.InvalidValueError):
            val.validate_batch(
                "daily", batch(bad_records), trade_date=trade_date, known_instrument_ids=known
            )
        assert facts.count_for_date("daily", trade_date) == 0
