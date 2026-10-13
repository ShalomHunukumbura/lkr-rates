from datetime import date, datetime
from decimal import Decimal

from lkr_rates.parse import Rate
from lkr_rates.store import MergeResult, Store

FIRST_RUN = datetime(2026, 10, 12, 13, 0)
SECOND_RUN = datetime(2026, 10, 13, 13, 0)


def usd(day: int, rate: str, rate_type: str = "indicative") -> Rate:
    return Rate(date(2026, 10, day), "USD", rate_type, Decimal(rate))


def test_merge_adds_new_rows(tmp_path):
    with Store(tmp_path / "rates.parquet") as store:
        assert store.merge([usd(8, "330.98"), usd(9, "330.96")], FIRST_RUN) == MergeResult(2, 0, 0)
        assert store.row_count() == 2


def test_running_twice_changes_nothing(tmp_path):
    path = tmp_path / "rates.parquet"
    rates = [usd(8, "330.98"), usd(9, "330.96")]
    with Store(path) as store:
        store.merge(rates, FIRST_RUN)
        store.save()
    first = path.read_bytes()

    with Store(path) as store:
        assert store.merge(rates, SECOND_RUN) == MergeResult(0, 0, 2)
        store.save()

    assert path.read_bytes() == first


def test_a_corrected_rate_replaces_the_old_one(tmp_path):
    with Store(tmp_path / "rates.parquet") as store:
        store.merge([usd(8, "330.98"), usd(9, "330.96")], FIRST_RUN)
        assert store.merge([usd(8, "330.98"), usd(9, "331.50")], SECOND_RUN) == MergeResult(0, 1, 1)
        rows = store.conn.execute(
            "SELECT rate, fetched_at FROM rates ORDER BY rate_date"
        ).fetchall()
    # The untouched row keeps its first-seen time; the corrected one gets the new time.
    assert rows == [(Decimal("330.98"), FIRST_RUN), (Decimal("331.50"), SECOND_RUN)]


def test_same_day_twice_in_one_page_keeps_the_last(tmp_path):
    with Store(tmp_path / "rates.parquet") as store:
        store.merge([usd(9, "330.00"), usd(9, "330.96")], FIRST_RUN)
        assert store.conn.execute("SELECT rate FROM rates").fetchall() == [(Decimal("330.96"),)]


def test_saved_table_loads_back(tmp_path):
    path = tmp_path / "rates.parquet"
    with Store(path) as store:
        store.merge([usd(9, "330.96")], FIRST_RUN)
        store.save()
    with Store(path) as store:
        assert store.row_count() == 1
        assert store.latest_date() == date(2026, 10, 9)


def test_lookup_falls_back_to_the_last_working_day(tmp_path):
    with Store(tmp_path / "rates.parquet") as store:
        store.merge([usd(9, "330.96"), usd(9, "326.44", "buy"), usd(12, "330.85")], FIRST_RUN)
        sunday = store.lookup(date(2026, 10, 11), "USD")
        assert sunday.rate_date == date(2026, 10, 9)
        assert sunday.rates == {"indicative": Decimal("330.96"), "buy": Decimal("326.44")}
        assert store.lookup(date(2026, 10, 1), "USD") is None
        assert store.lookup(date(2026, 10, 12), "EUR") is None
