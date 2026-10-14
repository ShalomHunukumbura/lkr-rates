# lkr-rates

A clean daily archive of Sri Lankan rupee exchange rates, built from the Central Bank of
Sri Lanka's published rates and refreshed every day.

The Central Bank publishes rates through a web form that returns HTML tables. That is fine
for looking up one date, and awkward for anything else. This project collects those rates
into one tidy table you can download or query.

## What you get

| File | What it is |
|---|---|
| `site/data/rates.csv` | Every date and currency, with `indicative`, `buy` and `sell` columns |
| `site/data/rates.parquet` | The same table, smaller and typed |
| `site/data/latest.json` | The newest day only |
| `site/data/quarantine.csv` | Published rates that are plainly wrong, left out of the files above, with the reason |
| `site/index.html` | A page with a chart and a "rate on this date" lookup |

Rates are rupees for one unit of the currency.

- **Indicative** is the Central Bank's middle rate, for 54 currencies and gold.
- **Buy** and **sell** are telegraphic transfer rates for nine major currencies.

Both start on 7 May 2010. As of 13 October 2026 the table has 249,911 rates.

## Run it

You need [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run lkr-rates backfill          # download all history, about 6 minutes
uv run lkr-rates update            # the daily run: last 10 days
uv run lkr-rates rate 2024-03-15 usd
uv run pytest
```

To see the page, serve the `site` folder:

```bash
python3 -m http.server --directory site 8000
```

## Commands

| Command | What it does |
|---|---|
| `backfill` | Downloads history one year at a time, then rebuilds the clean table |
| `update` | Downloads the last 10 days, merges them in, checks, exports |
| `rebuild` | Throws the clean table away and rebuilds it from the saved raw pages |
| `check` | Runs the data quality checks |
| `export` | Rewrites the files in `site/data` |
| `rate DATE CURRENCY` | Prints the rate on a date, or on the last working day before it |

## How it works

```
CBSL web forms -> data/raw/ (pages, untouched) -> data/clean/rates.parquet -> site/data/
                                                        ^
                                              checks must pass first
```

1. **Extract.** Submit the Central Bank's forms and save each page exactly as it arrives.
2. **Load.** Parse the pages into rows and merge them into the clean table. A row is
   identified by date, currency and rate type, so merging the same data twice changes
   nothing.
3. **Quarantine.** Move buy/sell pairs that cannot both be right (buy above sell, or sell
   far above buy) into a separate table, with the reason.
4. **Check.** Run the quality checks on the merged table. If one fails, the run stops and
   nothing is written.
5. **Publish.** Write the CSV, Parquet and JSON files the page uses.

`scripts/daily.sh` runs `update` and pushes any new rates. The push triggers a GitHub
Actions workflow that publishes the page. The update itself cannot run on GitHub Actions:
the Central Bank's site answers GitHub's servers with `403 Forbidden`. So the script runs
on a schedule from an ordinary machine instead.

[`explain.md`](explain.md) walks through all of this step by step, with the ideas behind it.

## Things to know about the data

- The Central Bank's form says its data starts in November 2006. It returns nothing before
  7 May 2010.
- 2010 to 2013 is patchy: the source has only 30 to 160 days a year, with gaps of up to two
  months. From 2014 on it is complete, at about 240 working days a year.
- 26 published buy/sell rates are plainly wrong at the source, for example a yen sell rate
  of 2,785.9084 on 25 March 2011. They are in `quarantine.csv`, not in the main table.
- Weekends and public holidays have no rates. The lookup falls back to the last working day.
- The Central Bank rounds to 4 decimal places. Weak currencies such as the Vietnamese dong
  have only three significant digits, and the Iranian rial now rounds to zero, so those
  days are left out.
- These are indicative rates. Your bank's rate will differ.

## Source and terms

Data comes from the [Central Bank of Sri Lanka](https://www.cbsl.gov.lk/en/rates-and-indicators/exchange-rates).
The pipeline waits 10 seconds between requests, as the site's `robots.txt` asks, and a daily
run makes four requests. The full backfill makes 36.

This is an unofficial archive and is not affiliated with the Central Bank. The Central
Bank's site is marked "All Rights Reserved" and does not state terms for reusing its data.
The code here is MIT licensed; that licence does not cover the data.
