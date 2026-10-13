from datetime import UTC, date, datetime

from lkr_rates import cli
from lkr_rates.raw import iter_raw, save_raw
from lkr_rates.store import Store, clean_path


def test_year_chunks_split_on_calendar_years():
    assert cli.year_chunks(date(2006, 11, 11), date(2008, 3, 5)) == [
        (date(2006, 11, 11), date(2006, 12, 31)),
        (date(2007, 1, 1), date(2007, 12, 31)),
        (date(2008, 1, 1), date(2008, 3, 5)),
    ]
    assert cli.year_chunks(date(2026, 10, 4), date(2026, 10, 13)) == [
        (date(2026, 10, 4), date(2026, 10, 13))
    ]


def save_fixture(root, page, series, fetched_at=datetime(2026, 10, 13, 4, 0, tzinfo=UTC)):
    html, currencies = page(series)
    return save_raw(
        root, series, date(2026, 9, 30), date(2026, 10, 13), html, currencies, fetched_at
    )


def test_raw_file_comes_back_exactly_as_saved(tmp_path, page):
    html, currencies = page("tt")
    save_fixture(tmp_path, page, "tt")
    (raw,) = iter_raw(tmp_path)
    assert raw.html() == html
    assert raw.currencies == currencies


def test_rebuild_makes_the_clean_table_and_exports(tmp_path, page, capsys):
    save_fixture(tmp_path, page, "indicative")
    save_fixture(tmp_path, page, "tt")
    argv = ["--root", str(tmp_path), "--today", "2026-10-13", "rebuild"]

    assert cli.main(argv) == 0
    first = clean_path(tmp_path).read_bytes()
    assert (tmp_path / "site/data/rates.csv").exists()
    assert (tmp_path / "site/data/by-currency/USD.json").exists()

    # Rebuilding from the same raw files gives the same file, byte for byte.
    assert cli.main(argv) == 0
    assert clean_path(tmp_path).read_bytes() == first


def test_failed_check_writes_nothing(tmp_path, page, capsys):
    save_fixture(tmp_path, page, "indicative")
    # Three weeks later this data is stale, so the freshness check fails.
    assert cli.main(["--root", str(tmp_path), "--today", "2026-11-05", "rebuild"]) == 1
    assert not clean_path(tmp_path).exists()
    assert "nothing was written" in capsys.readouterr().out


def test_rate_command_falls_back_from_a_weekend(tmp_path, page, capsys):
    save_fixture(tmp_path, page, "indicative")
    cli.main(["--root", str(tmp_path), "--today", "2026-10-13", "rebuild"])
    capsys.readouterr()

    assert cli.main(["--root", str(tmp_path), "rate", "2026-10-11", "usd"]) == 0
    out = capsys.readouterr().out
    assert "showing 2026-10-09" in out
    assert "1 USD = 330.9617 LKR (indicative)" in out

    with Store(clean_path(tmp_path)) as store:
        assert store.latest_date() == date(2026, 10, 13)
