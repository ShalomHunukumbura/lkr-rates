"""The clean layer: one tidy table of rates, kept in a Parquet file.

DuckDB does the work in memory. We read the Parquet file in, merge new rows,
check the result, and only then write the file back.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Self

import duckdb

from .parse import Rate

SCHEMA = """
CREATE TABLE rates (
    rate_date  DATE          NOT NULL,
    currency   VARCHAR       NOT NULL,
    rate_type  VARCHAR       NOT NULL,   -- indicative, buy or sell
    rate       DECIMAL(18,6) NOT NULL,   -- rupees for one unit of the currency
    fetched_at TIMESTAMP     NOT NULL,   -- when we first saw this value
    PRIMARY KEY (rate_date, currency, rate_type)
);
-- Rows the source got wrong. Same shape as rates, plus why we set them aside.
CREATE TABLE quarantine (
    rate_date  DATE          NOT NULL,
    currency   VARCHAR       NOT NULL,
    rate_type  VARCHAR       NOT NULL,
    rate       DECIMAL(18,6) NOT NULL,
    fetched_at TIMESTAMP     NOT NULL,
    reason     VARCHAR       NOT NULL,
    PRIMARY KEY (rate_date, currency, rate_type)
);
"""


def clean_path(root: Path) -> Path:
    return root / "data" / "clean" / "rates.parquet"


def quarantine_path(root: Path) -> Path:
    return root / "data" / "clean" / "quarantine.csv"


@dataclass(frozen=True)
class MergeResult:
    new: int
    changed: int
    unchanged: int

    def __add__(self, other: MergeResult) -> MergeResult:
        return MergeResult(
            self.new + other.new, self.changed + other.changed, self.unchanged + other.unchanged
        )


@dataclass(frozen=True)
class Lookup:
    asked_for: date
    rate_date: date  # the day the rate is actually from
    currency: str
    rates: dict[str, Decimal]


class Store:
    """The rates table, loaded into DuckDB for the length of one run."""

    def __init__(self, path: Path, load: bool = True):
        self.path = path
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(SCHEMA)
        self.quarantine_path = path.with_name("quarantine.csv")
        if load and path.exists():
            self.conn.execute("INSERT INTO rates SELECT * FROM read_parquet(?)", [str(path)])
        if load and self.quarantine_path.exists():
            self.conn.execute(
                """
                INSERT INTO quarantine SELECT * FROM read_csv(?, header = true, columns = {
                    'rate_date': 'DATE', 'currency': 'VARCHAR', 'rate_type': 'VARCHAR',
                    'rate': 'DECIMAL(18,6)', 'fetched_at': 'TIMESTAMP', 'reason': 'VARCHAR'})
                """,
                [str(self.quarantine_path)],
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.conn.close()

    def merge(self, rates: Iterable[Rate], fetched_at: datetime) -> MergeResult:
        """Add new rates and replace changed ones. Running it twice changes nothing."""
        with tempfile.TemporaryDirectory() as folder:
            incoming = Path(folder) / "incoming.csv"
            with open(incoming, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["rate_date", "currency", "rate_type", "rate"])
                for r in rates:
                    writer.writerow([r.rate_date.isoformat(), r.currency, r.rate_type, r.rate])
            # If a page lists the same day twice, the last one wins.
            self.conn.execute(
                """
                CREATE OR REPLACE TEMP TABLE incoming AS
                SELECT rate_date, currency, rate_type, rate
                FROM (
                    SELECT *, row_number() OVER () AS position
                    FROM read_csv(?, header = true, columns = {
                        'rate_date': 'DATE', 'currency': 'VARCHAR',
                        'rate_type': 'VARCHAR', 'rate': 'DECIMAL(18,6)'})
                )
                QUALIFY row_number() OVER (
                    PARTITION BY rate_date, currency, rate_type ORDER BY position DESC) = 1
                """,
                [str(incoming)],
            )
        new, changed, unchanged = self.conn.execute(
            """
            SELECT count(*) FILTER (WHERE r.rate IS NULL),
                   count(*) FILTER (WHERE r.rate IS NOT NULL AND r.rate <> i.rate),
                   count(*) FILTER (WHERE r.rate = i.rate)
            FROM incoming i
            LEFT JOIN rates r USING (rate_date, currency, rate_type)
            """
        ).fetchone()
        # Rows whose value did not change are left alone, fetched_at included.
        self.conn.execute(
            """
            INSERT OR REPLACE INTO rates
            SELECT i.rate_date, i.currency, i.rate_type, i.rate, ?
            FROM incoming i
            LEFT JOIN rates r USING (rate_date, currency, rate_type)
            WHERE r.rate IS NULL OR r.rate <> i.rate
            """,
            [fetched_at.replace(tzinfo=None)],
        )
        return MergeResult(new, changed, unchanged)

    def save(self) -> None:
        """Write the table to disk. The old file is replaced in one step."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".parquet.tmp")
        self.conn.execute(
            f"""
            COPY (SELECT * FROM rates ORDER BY rate_date, currency, rate_type)
            TO '{temporary}' (FORMAT parquet, COMPRESSION zstd)
            """
        )
        os.replace(temporary, self.path)
        temporary = self.quarantine_path.with_suffix(".csv.tmp")
        self.conn.execute(
            f"""
            COPY (SELECT * FROM quarantine ORDER BY rate_date, currency, rate_type)
            TO '{temporary}' (HEADER)
            """
        )
        os.replace(temporary, self.quarantine_path)

    def row_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM rates").fetchone()[0]

    def latest_date(self) -> date | None:
        return self.conn.execute("SELECT max(rate_date) FROM rates").fetchone()[0]

    def lookup(self, day: date, currency: str) -> Lookup | None:
        """The rate on a day, or on the last working day before it."""
        rows = self.conn.execute(
            """
            SELECT rate_date, rate_type, rate FROM rates
            WHERE currency = ? AND rate_date = (
                SELECT max(rate_date) FROM rates WHERE currency = ? AND rate_date <= ?)
            """,
            [currency, currency, day],
        ).fetchall()
        if not rows:
            return None
        return Lookup(day, rows[0][0], currency, {rate_type: rate for _, rate_type, rate in rows})
