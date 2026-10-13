"""Writes the files people download and the small files the web page reads."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from .raw import iter_raw

# One row per date and currency, with the three rate types side by side.
WIDE = """
SELECT rate_date AS date,
       currency,
       max(rate) FILTER (WHERE rate_type = 'indicative') AS indicative,
       max(rate) FILTER (WHERE rate_type = 'buy')        AS buy,
       max(rate) FILTER (WHERE rate_type = 'sell')       AS sell
FROM rates
GROUP BY rate_date, currency
"""


def currency_names(root: Path) -> dict[str, str]:
    """{code: name}, taken from the forms we downloaded from."""
    names: dict[str, str] = {}
    for raw in iter_raw(root):
        for name, code in raw.currencies.items():
            # The indicative form has the fuller list and the clearer names.
            if raw.series == "indicative" or code not in names:
                names[code] = name
    return names


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")) + "\n")


def export(conn: duckdb.DuckDBPyConnection, root: Path) -> Path:
    out = root / "site" / "data"
    by_currency = out / "by-currency"
    by_currency.mkdir(parents=True, exist_ok=True)

    conn.execute(f"CREATE OR REPLACE TEMP VIEW wide AS {WIDE}")
    order = "ORDER BY date, currency"
    conn.execute(
        f"COPY (SELECT * FROM wide {order}) TO '{out / 'rates.parquet'}' "
        "(FORMAT parquet, COMPRESSION zstd)"
    )
    conn.execute(f"COPY (SELECT * FROM wide {order}) TO '{out / 'rates.csv'}' (HEADER)")

    conn.execute(
        f"COPY (SELECT * FROM quarantine ORDER BY rate_date, currency, rate_type) "
        f"TO '{out / 'quarantine.csv'}' (HEADER)"
    )

    names = currency_names(root)
    summary = conn.execute(
        """
        SELECT currency, min(date), max(date), count(*), count(buy) > 0
        FROM wide GROUP BY currency ORDER BY currency
        """
    ).fetchall()
    _write_json(
        out / "currencies.json",
        [
            {
                "code": code,
                "name": names.get(code, code),
                "from": first.isoformat(),
                "to": last.isoformat(),
                "days": days,
                "has_buy_sell": has_buy_sell,
            }
            for code, first, last, days, has_buy_sell in summary
        ],
    )

    def number(value: object) -> float | None:
        return None if value is None else float(value)

    for code, *_ in summary:
        rows = conn.execute(
            "SELECT date, indicative, buy, sell FROM wide WHERE currency = ? ORDER BY date", [code]
        ).fetchall()
        _write_json(
            by_currency / f"{code}.json",
            {
                "currency": code,
                "dates": [day.isoformat() for day, *_ in rows],
                "indicative": [number(r[1]) for r in rows],
                "buy": [number(r[2]) for r in rows],
                "sell": [number(r[3]) for r in rows],
            },
        )

    newest = conn.execute("SELECT max(date) FROM wide").fetchone()[0]
    latest = conn.execute(
        "SELECT currency, indicative, buy, sell FROM wide WHERE date = ? ORDER BY currency",
        [newest],
    ).fetchall()
    _write_json(
        out / "latest.json",
        {
            "date": newest.isoformat() if newest else None,
            "source": "Central Bank of Sri Lanka",
            "rates": {
                code: {"indicative": number(mid), "buy": number(buy), "sell": number(sell)}
                for code, mid, buy, sell in latest
            },
        },
    )
    return out
