from datetime import date
from decimal import Decimal

import pytest

from lkr_rates.parse import ParseError, parse_rates


def test_indicative_page(page):
    html, currencies = page("indicative")
    rates = parse_rates(html, "indicative", currencies)
    by_key = {(r.rate_date, r.currency): r.rate for r in rates}

    assert {r.rate_type for r in rates} == {"indicative"}
    assert by_key[(date(2026, 10, 12), "USD")] == Decimal("330.8514")
    assert by_key[(date(2026, 10, 13), "JPY")] == Decimal("2.0918")
    # Gold is in the list too, priced per troy ounce.
    assert by_key[(date(2026, 10, 13), "XAU")] == Decimal("1380372.7296")


def test_rate_that_rounds_to_zero_is_dropped(page):
    # CBSL prints the Iranian rial as 0.0000. That is not a rate.
    html, currencies = page("indicative")
    rates = parse_rates(html, "indicative", currencies)
    assert "IRR" in currencies.values()
    assert not [r for r in rates if r.currency == "IRR"]
    assert all(r.rate > 0 for r in rates)


def test_buy_sell_page(page):
    html, currencies = page("tt")
    rates = parse_rates(html, "tt", currencies)
    by_key = {(r.rate_date, r.currency, r.rate_type): r.rate for r in rates}

    assert {r.currency for r in rates} == set(currencies.values())
    assert by_key[(date(2026, 10, 12), "USD", "buy")] == Decimal("326.4751")
    assert by_key[(date(2026, 10, 12), "USD", "sell")] == Decimal("335.4587")


def test_weekends_have_no_rows(page):
    html, currencies = page("tt")
    days = {r.rate_date for r in parse_rates(html, "tt", currencies)}
    assert date(2026, 10, 10) not in days  # a Saturday
    assert date(2026, 10, 9) in days


def test_unknown_currency_name_stops_the_run(page):
    html, _ = page("tt")
    with pytest.raises(ParseError, match="unknown currency name"):
        parse_rates(html, "tt", {"Some Other Name": "USD"})


def test_changed_table_layout_stops_the_run(page):
    html, currencies = page("tt")
    with pytest.raises(ParseError, match="unexpected buy/sell headers"):
        parse_rates(html.replace("Buy Rate (LKR)", "Mid Rate"), "tt", currencies)


def test_bad_number_stops_the_run(page):
    html, currencies = page("tt")
    with pytest.raises(ParseError, match="not a number"):
        parse_rates(html.replace("326.4751", "326.47x1"), "tt", currencies)


def test_page_with_no_tables_gives_no_rows():
    assert parse_rates("<html><body>No data</body></html>", "tt", {}) == []
