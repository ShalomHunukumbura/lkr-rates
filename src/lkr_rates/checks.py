"""Data quality checks, and the quarantine for rows the source got wrong.

Each check is one SQL query that returns the rows that are wrong. No rows
means the check passed. A failed "error" check stops the run before anything
is written; a "warning" is printed and the run carries on.

The quarantine is for a different case: old rows where the Central Bank's own
page is plainly wrong. Those are moved out of the rates table into a
quarantine table, with the reason, so they are neither published nor lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

# The longest run of days without a rate that we accept as holidays.
# The April New Year break plus a weekend can reach five days.
MAX_GAP_DAYS = 7
# How old the newest rate may be before we call the data stale.
MAX_STALE_DAYS = 5
# CBSL's history is patchy before this date (whole weeks are missing at the
# source), so the gap check only applies from here on.
COMPLETE_FROM = "2014-01-01"
# Sell is normally 2-9% above buy. Far beyond that, one of the two is a typo.
MAX_SPREAD = 0.25
# A quarantined row this recent is not an old source typo. It needs a person.
QUARANTINE_ALERT_DAYS = 30
# A one-day move bigger than this is worth a second look (the rupee moved
# about this much when it was floated in March 2022, so it is only a warning).
MAX_DAILY_MOVE = 0.20


@dataclass(frozen=True)
class Check:
    name: str
    level: str  # error or warning
    sql: str
    explain: str


@dataclass(frozen=True)
class CheckResult:
    check: Check
    bad_rows: int
    examples: list[tuple]

    @property
    def passed(self) -> bool:
        return self.bad_rows == 0


CHECKS = [
    Check(
        "no duplicate rows",
        "error",
        """
        SELECT rate_date, currency, rate_type, count(*) AS copies
        FROM rates GROUP BY ALL HAVING count(*) > 1
        """,
        "one date + currency + rate type must appear once",
    ),
    Check(
        "rates are positive",
        "error",
        "SELECT rate_date, currency, rate_type, rate FROM rates WHERE rate <= 0",
        "an exchange rate can't be zero or negative",
    ),
    Check(
        "codes and types are valid",
        "error",
        """
        SELECT rate_date, currency, rate_type FROM rates
        WHERE NOT regexp_full_match(currency, '[A-Z]{3}')
           OR rate_type NOT IN ('indicative', 'buy', 'sell')
        """,
        "currency must be a 3-letter code and type one of indicative, buy, sell",
    ),
    Check(
        "no dates in the future",
        "error",
        "SELECT rate_date, currency, rate_type FROM rates WHERE rate_date > $today",
        "a rate dated after today means a date was misread",
    ),
    Check(
        "buy is below sell",
        "error",
        """
        SELECT b.rate_date, b.currency, b.rate AS buy, s.rate AS sell
        FROM rates b JOIN rates s USING (rate_date, currency)
        WHERE b.rate_type = 'buy' AND s.rate_type = 'sell' AND b.rate >= s.rate
        """,
        "banks buy a currency for less than they sell it",
    ),
    Check(
        "data is fresh",
        "error",
        f"""
        SELECT max(rate_date) AS newest, $today AS today
        FROM rates WHERE currency = 'USD' AND rate_type = 'indicative'
        HAVING max(rate_date) IS NULL OR max(rate_date) < $today - INTERVAL {MAX_STALE_DAYS} DAY
        """,
        f"the newest USD rate should be at most {MAX_STALE_DAYS} days old",
    ),
    Check(
        "no long gaps in USD",
        "error",
        f"""
        SELECT previous_date, rate_date, rate_date - previous_date AS days
        FROM (
            SELECT rate_date, lag(rate_date) OVER (ORDER BY rate_date) AS previous_date
            FROM rates WHERE currency = 'USD' AND rate_type = 'indicative'
        )
        WHERE rate_date - previous_date > {MAX_GAP_DAYS} AND rate_date >= DATE '{COMPLETE_FROM}'
        ORDER BY rate_date
        """,
        f"more than {MAX_GAP_DAYS} days without a USD rate means days are missing",
    ),
    Check(
        "nothing quarantined recently",
        "error",
        f"""
        SELECT rate_date, currency, rate_type, rate, reason FROM quarantine
        WHERE rate_date > $today - INTERVAL {QUARANTINE_ALERT_DAYS} DAY
        ORDER BY rate_date DESC
        """,
        "a fresh row failing a basic rule is more likely our bug than an old source typo",
    ),
    Check(
        "no wild one-day moves",
        "warning",
        f"""
        SELECT rate_date, currency, rate_type, previous, rate
        FROM (
            SELECT *, lag(rate) OVER (
                PARTITION BY currency, rate_type ORDER BY rate_date) AS previous
            FROM rates
        )
        WHERE abs(rate - previous) / previous > {MAX_DAILY_MOVE}
        ORDER BY rate_date DESC
        """,
        f"a move of more than {MAX_DAILY_MOVE:.0%} in a day is rare; check it is real",
    ),
    Check(
        "newest day has every currency",
        "warning",
        """
        WITH days AS (
            SELECT DISTINCT rate_date FROM rates WHERE rate_type = 'indicative'
            ORDER BY rate_date DESC LIMIT 2
        )
        SELECT currency FROM rates
        WHERE rate_type = 'indicative' AND rate_date = (SELECT min(rate_date) FROM days)
        EXCEPT
        SELECT currency FROM rates
        WHERE rate_type = 'indicative' AND rate_date = (SELECT max(rate_date) FROM days)
        """,
        "a currency that was there on the previous day is missing on the newest day",
    ),
]


# Each rule finds buy/sell pairs that cannot both be right. We can't tell
# which of the two is the wrong one, so both go to quarantine.
QUARANTINE_RULES = {
    "buy rate is not below sell rate": "buy >= sell",
    f"sell rate is more than {MAX_SPREAD:.0%} above buy rate": f"sell > buy * {1 + MAX_SPREAD}",
}


def quarantine(conn: duckdb.DuckDBPyConnection) -> int:
    """Move impossible buy/sell pairs out of rates. Returns how many rows moved."""
    rules = " UNION ALL ".join(
        f"SELECT rate_date, currency, '{reason}' AS reason FROM pairs WHERE {condition}"
        for reason, condition in QUARANTINE_RULES.items()
    )
    conn.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE bad AS
        WITH pairs AS (
            SELECT rate_date, currency, b.rate AS buy, s.rate AS sell
            FROM rates b JOIN rates s USING (rate_date, currency)
            WHERE b.rate_type = 'buy' AND s.rate_type = 'sell'
        )
        SELECT rate_date, currency, min(reason) AS reason FROM ({rules})
        GROUP BY rate_date, currency
        """
    )
    moved = conn.execute(
        """
        INSERT OR REPLACE INTO quarantine
        SELECT r.*, bad.reason FROM rates r JOIN bad USING (rate_date, currency)
        WHERE r.rate_type IN ('buy', 'sell')
        RETURNING 1
        """
    ).fetchall()
    conn.execute(
        """
        DELETE FROM rates
        WHERE rate_type IN ('buy', 'sell')
          AND (rate_date, currency) IN (SELECT (rate_date, currency) FROM bad)
        """
    )
    # If the source later publishes a corrected rate, the row is back in rates
    # and no longer belongs in quarantine.
    conn.execute(
        """
        DELETE FROM quarantine q
        WHERE EXISTS (SELECT 1 FROM rates r WHERE r.rate_date = q.rate_date
                      AND r.currency = q.currency AND r.rate_type = q.rate_type)
        """
    )
    return len(moved)


def run_checks(conn: duckdb.DuckDBPyConnection, today: date) -> list[CheckResult]:
    results = []
    for check in CHECKS:
        params = {"today": today} if "$today" in check.sql else {}
        rows = conn.execute(check.sql, params).fetchall()
        results.append(CheckResult(check, len(rows), rows[:5]))
    return results


def has_errors(results: list[CheckResult]) -> bool:
    return any(not r.passed and r.check.level == "error" for r in results)


def report(results: list[CheckResult]) -> str:
    lines = []
    for result in results:
        if result.passed:
            lines.append(f"  ok       {result.check.name}")
            continue
        label = "FAILED " if result.check.level == "error" else "warning"
        lines.append(f"  {label}  {result.check.name}: {result.bad_rows} row(s)")
        lines.append(f"           why it matters: {result.check.explain}")
        for row in result.examples:
            lines.append(f"           {', '.join(str(value) for value in row)}")
    return "\n".join(lines)
