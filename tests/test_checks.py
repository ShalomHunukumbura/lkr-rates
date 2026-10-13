from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from lkr_rates.checks import has_errors, quarantine, run_checks
from lkr_rates.parse import Rate
from lkr_rates.store import Store

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 13, 0)


def good_rates() -> list[Rate]:
    rates = []
    for back in range(10):
        day = TODAY - timedelta(days=back)
        if day.weekday() < 5:
            rates += [
                Rate(day, "USD", "indicative", Decimal("330.9")),
                Rate(day, "USD", "buy", Decimal("326.4")),
                Rate(day, "USD", "sell", Decimal("335.4")),
            ]
    return rates


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "rates.parquet") as store:
        store.merge(good_rates(), NOW)
        yield store


def failed(store: Store, today: date = TODAY) -> set[str]:
    return {r.check.name for r in run_checks(store.conn, today) if not r.passed}


def test_good_data_passes(store):
    assert failed(store) == set()
    assert not has_errors(run_checks(store.conn, TODAY))


def test_zero_rate_is_caught(store):
    store.merge([Rate(TODAY, "IRR", "indicative", Decimal(0))], NOW)
    assert "rates are positive" in failed(store)
    assert has_errors(run_checks(store.conn, TODAY))


def test_buy_above_sell_is_caught(store):
    store.merge([Rate(TODAY, "USD", "buy", Decimal(340))], NOW)
    assert "buy is below sell" in failed(store)


def test_future_date_is_caught(store):
    store.merge([Rate(TODAY + timedelta(days=30), "USD", "indicative", Decimal(330))], NOW)
    assert "no dates in the future" in failed(store)


def test_bad_currency_code_is_caught(store):
    store.merge([Rate(TODAY, "US Dollar", "indicative", Decimal(330))], NOW)
    assert "codes and types are valid" in failed(store)


def test_stale_data_is_caught(store):
    assert "data is fresh" in failed(store, today=TODAY + timedelta(days=20))


def test_empty_table_is_not_fresh(tmp_path):
    with Store(tmp_path / "empty.parquet") as empty:
        assert "data is fresh" in failed(empty)


def test_missing_weeks_are_caught(store):
    store.merge([Rate(TODAY - timedelta(days=40), "USD", "indicative", Decimal(329))], NOW)
    assert "no long gaps in USD" in failed(store)


def test_wild_move_is_only_a_warning(store):
    store.merge([Rate(TODAY, "USD", "indicative", Decimal(500))], NOW)
    results = run_checks(store.conn, TODAY)
    assert "no wild one-day moves" in {r.check.name for r in results if not r.passed}
    assert not has_errors(results)


def test_currency_missing_on_newest_day_is_a_warning(store):
    store.merge([Rate(TODAY - timedelta(days=1), "EUR", "indicative", Decimal(370))], NOW)
    results = run_checks(store.conn, TODAY)
    assert "newest day has every currency" in {r.check.name for r in results if not r.passed}
    assert not has_errors(results)


def pair(day: date, currency: str, buy: str, sell: str) -> list[Rate]:
    return [Rate(day, currency, "buy", Decimal(buy)), Rate(day, currency, "sell", Decimal(sell))]


def held(store: Store) -> list[tuple]:
    return store.conn.execute(
        "SELECT rate_date, currency, rate_type, reason FROM quarantine ORDER BY ALL"
    ).fetchall()


OLD = date(2011, 3, 25)


def test_good_pairs_are_not_quarantined(store):
    assert quarantine(store.conn) == 0
    assert held(store) == []


def test_swapped_buy_and_sell_go_to_quarantine(store):
    store.merge(pair(OLD, "EUR", "148.98", "144.85"), NOW)
    assert quarantine(store.conn) == 2
    assert held(store) == [
        (OLD, "EUR", "buy", "buy rate is not below sell rate"),
        (OLD, "EUR", "sell", "buy rate is not below sell rate"),
    ]
    assert store.conn.execute("SELECT count(*) FROM rates WHERE currency = 'EUR'").fetchone() == (
        0,
    )
    assert "buy is below sell" not in failed(store)


def test_absurd_sell_rate_goes_to_quarantine(store):
    # CBSL really published a yen sell rate of 2785.9084 on this day.
    store.merge(pair(OLD, "JPY", "1.3439", "2785.9084"), NOW)
    quarantine(store.conn)
    assert {row[3] for row in held(store)} == {"sell rate is more than 25% above buy rate"}


def test_old_quarantined_rows_do_not_block_the_run(store):
    store.merge(pair(OLD, "JPY", "1.3439", "2785.9084"), NOW)
    quarantine(store.conn)
    assert not has_errors(run_checks(store.conn, TODAY))


def test_fresh_quarantined_row_blocks_the_run(store):
    store.merge(pair(TODAY, "USD", "340", "335"), NOW)
    quarantine(store.conn)
    assert "nothing quarantined recently" in failed(store)
    assert has_errors(run_checks(store.conn, TODAY))


def test_corrected_row_leaves_quarantine(store):
    store.merge(pair(OLD, "EUR", "148.98", "144.85"), NOW)
    quarantine(store.conn)
    store.merge(pair(OLD, "EUR", "141.20", "144.85"), NOW)
    assert quarantine(store.conn) == 0
    assert held(store) == []


def test_quarantine_survives_save_and_load(tmp_path):
    path = tmp_path / "rates.parquet"
    with Store(path) as first:
        first.merge(good_rates() + pair(OLD, "EUR", "148.98", "144.85"), NOW)
        quarantine(first.conn)
        first.save()
    with Store(path) as second:
        assert len(held(second)) == 2
        assert quarantine(second.conn) == 0


def test_gaps_before_2014_are_allowed(store):
    store.merge(
        [
            Rate(date(2012, 4, 4), "USD", "indicative", Decimal(128)),
            Rate(date(2012, 6, 8), "USD", "indicative", Decimal(131)),
            Rate(date(2013, 12, 31), "USD", "indicative", Decimal(130)),
        ],
        NOW,
    )
    # The 2012 hole is tolerated; the jump from 2013 to 2026 is not.
    gaps = next(r for r in run_checks(store.conn, TODAY) if r.check.name == "no long gaps in USD")
    assert gaps.bad_rows == 1
