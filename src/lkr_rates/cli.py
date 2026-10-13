"""The command line: `lkr-rates update`, `backfill`, `rebuild`, `check`, `export`, `rate`."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .checks import has_errors, quarantine, report, run_checks
from .export import export
from .parse import parse_rates
from .raw import RawFile, has_range, iter_raw, save_raw
from .source import EARLIEST_DATE, SERIES, Client
from .store import MergeResult, Store, clean_path

COLOMBO = ZoneInfo("Asia/Colombo")
# Each daily run re-reads the last few days, so a missed run fixes itself
# and a rate CBSL corrects later is picked up.
DEFAULT_WINDOW_DAYS = 10


def today_in_colombo() -> date:
    return datetime.now(COLOMBO).date()


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into pieces of at most one calendar year."""
    chunks = []
    while start <= end:
        chunk_end = min(date(start.year, 12, 31), end)
        chunks.append((start, chunk_end))
        start = chunk_end + timedelta(days=1)
    return chunks


def download(root: Path, client: Client, ranges: list[tuple[date, date]], today: date) -> None:
    """Fetch each range for both series and save the pages untouched."""
    for series in SERIES.values():
        currencies = None
        for start, end in ranges:
            # A finished past range never changes, so one copy is enough.
            if end < today and has_range(root, series.name, start, end):
                print(f"  have   {series.name:<10} {start} to {end}")
                continue
            if currencies is None:
                currencies = client.currencies(series)
            html = client.fetch(series, start, end, currencies)
            raw = save_raw(root, series.name, start, end, html, currencies, datetime.now(UTC))
            print(f"  saved  {series.name:<10} {start} to {end}  ({raw.path.name})")


def merge_raw(store: Store, files: list[RawFile]) -> MergeResult:
    total = MergeResult(0, 0, 0)
    for raw in files:
        rates = parse_rates(raw.html(), raw.series, raw.currencies)
        total += store.merge(rates, raw.fetched_at)
    return total


def finish(store: Store, root: Path, today: date, merged: MergeResult | None) -> int:
    """Check the table. Save and export only if it is good."""
    if merged is not None:
        print(f"merged: {merged.new} new, {merged.changed} changed, {merged.unchanged} unchanged")
    moved = quarantine(store.conn)
    held = store.conn.execute("SELECT count(*) FROM quarantine").fetchone()[0]
    print(f"quarantine: {moved} row(s) moved in this run, {held} held in total")
    results = run_checks(store.conn, today)
    print("checks:")
    print(report(results))
    if has_errors(results):
        print("A check failed, so nothing was written. The raw files are kept for debugging.")
        return 1
    store.save()
    out = export(store.conn, root)
    print(f"saved {store.row_count()} rows up to {store.latest_date()}; exports in {out}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    today = args.today
    start = today - timedelta(days=args.window - 1)
    print(f"update: {start} to {today}")
    before = {raw.path for raw in iter_raw(args.root)}
    download(args.root, Client(delay=args.delay), [(start, today)], today)
    new_files = [raw for raw in iter_raw(args.root) if raw.path not in before]
    with Store(clean_path(args.root)) as store:
        return finish(store, args.root, today, merge_raw(store, new_files))


def cmd_backfill(args: argparse.Namespace) -> int:
    today = args.today
    end = min(args.to or today, today)
    print(f"backfill: {args.start} to {end}")
    download(args.root, Client(delay=args.delay), year_chunks(args.start, end), today)
    return cmd_rebuild(args)


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Throw the clean table away and build it again from the raw files."""
    files = list(iter_raw(args.root))
    print(f"rebuild: reading {len(files)} raw file(s)")
    with Store(clean_path(args.root), load=False) as store:
        return finish(store, args.root, args.today, merge_raw(store, files))


def cmd_check(args: argparse.Namespace) -> int:
    with Store(clean_path(args.root)) as store:
        results = run_checks(store.conn, args.today)
    print(report(results))
    return 1 if has_errors(results) else 0


def cmd_export(args: argparse.Namespace) -> int:
    with Store(clean_path(args.root)) as store:
        print(f"exports in {export(store.conn, args.root)}")
    return 0


def cmd_rate(args: argparse.Namespace) -> int:
    with Store(clean_path(args.root)) as store:
        found = store.lookup(args.date, args.currency.upper())
    if found is None:
        print(f"no {args.currency.upper()} rate on or before {args.date}")
        return 1
    if found.rate_date != found.asked_for:
        print(f"no rate published on {found.asked_for}; showing {found.rate_date}")
    for rate_type in ("indicative", "buy", "sell"):
        if rate_type in found.rates:
            print(f"1 {found.currency} = {found.rates[rate_type]:.4f} LKR ({rate_type})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lkr-rates", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project folder")
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=today_in_colombo(),
        help="pretend today is this date (for testing)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def with_delay(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--delay", type=float, default=Client().delay, help="seconds between requests"
        )

    update = commands.add_parser("update", help="the daily run: fetch recent days, merge, check")
    update.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS, help="days to re-read")
    with_delay(update)
    update.set_defaults(run=cmd_update)

    backfill = commands.add_parser("backfill", help="download history, one year at a time")
    backfill.add_argument("--from", dest="start", type=date.fromisoformat, default=EARLIEST_DATE)
    backfill.add_argument("--to", type=date.fromisoformat, default=None)
    with_delay(backfill)
    backfill.set_defaults(run=cmd_backfill)

    for name, run, text in (
        ("rebuild", cmd_rebuild, "rebuild the clean table from the raw files"),
        ("check", cmd_check, "run the data quality checks"),
        ("export", cmd_export, "write the download and web page files"),
    ):
        commands.add_parser(name, help=text).set_defaults(run=run)

    rate = commands.add_parser("rate", help="look up the rate on a date")
    rate.add_argument("date", type=date.fromisoformat)
    rate.add_argument("currency")
    rate.set_defaults(run=cmd_rate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
