"""Talks to the Central Bank of Sri Lanka's exchange-rate lookup pages.

CBSL has no API. It has two small web forms: you pick a date range and some
currencies, submit, and get an HTML page with one table per currency. This
module submits those forms the same way a browser would and hands back the
HTML exactly as it arrived.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

BASE_URL = "https://www.cbsl.gov.lk/cbsl_custom"
USER_AGENT = os.environ.get(
    "LKR_RATES_USER_AGENT",
    "lkr-rates/0.1 (+https://github.com/ShalomHunukumbura/lkr-rates)",
)
# cbsl.gov.lk/robots.txt asks for 10 seconds between requests.
CRAWL_DELAY_SECONDS = 10.0
# The forms claim to start here, and send this value along with every request.
FORM_START_RANGE = date(2006, 11, 11)
# In practice the forms return nothing before May 2010, so the backfill starts here.
EARLIEST_DATE = date(2010, 1, 1)


@dataclass(frozen=True)
class Series:
    """One of the two rate tables CBSL publishes."""

    name: str
    form_path: str
    results_path: str


SERIES = {
    # One middle rate per currency, about 55 currencies plus gold.
    "indicative": Series("indicative", "exrates/exrates.php", "exrates/exrates_results.php"),
    # Buy and sell rates (telegraphic transfer) for 9 major currencies.
    "tt": Series("tt", "exratestt/exratestt.php", "exratestt/exrates_resultstt.php"),
}

_CURRENCY_OPTION = re.compile(r'value="([A-Z]{3})~([^"]+)"')


class SourceError(RuntimeError):
    """The source could not be reached or answered with something unusable."""


def normalise_name(name: str) -> str:
    """Collapse runs of whitespace so 'Malaysia  Ringgit' matches everywhere."""
    return " ".join(name.split())


class Client:
    """A polite HTTP client: one request at a time, with a pause in between."""

    def __init__(self, delay: float = CRAWL_DELAY_SECONDS, retries: int = 3, timeout: float = 60.0):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last_request_at: float | None = None

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            remaining = self.delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)

    def _request(self, path: str, form: list[tuple[str, str]] | None = None) -> str:
        url = f"{BASE_URL}/{path}"
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_for_turn()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
            finally:
                self._last_request_at = time.monotonic()
        raise SourceError(f"{url} failed after {self.retries} attempts: {last_error}")

    def currencies(self, series: Series) -> dict[str, str]:
        """Read the form and return every currency it offers, as {name: code}."""
        html = self._request(series.form_path)
        found = {normalise_name(name): code for code, name in _CURRENCY_OPTION.findall(html)}
        if not found:
            raise SourceError(
                f"no currencies found on the {series.name} form; has the page changed?"
            )
        return found

    def fetch(self, series: Series, start: date, end: date, currencies: dict[str, str]) -> str:
        """Submit the form for a date range and return the result page as-is."""
        form = [
            ("lookupPage", "lookup_daily_exchange_rates.php"),
            ("startRange", FORM_START_RANGE.isoformat()),
            ("rangeType", "dates"),
            ("txtStart", start.isoformat()),
            ("txtEnd", end.isoformat()),
        ]
        form += [("chk_cur[]", f"{code}~{name}") for name, code in currencies.items()]
        form.append(("submit_button", "Submit"))
        return self._request(series.results_path, form)
