"""Turns a CBSL result page into plain rows.

Nothing here touches the network or the disk. It takes HTML text in and gives
rows out, which makes it easy to test with saved pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .source import normalise_name


class ParseError(ValueError):
    """The page did not look the way we expect. Better to stop than to guess."""


@dataclass(frozen=True)
class Rate:
    rate_date: date
    currency: str  # ISO code, e.g. USD
    rate_type: str  # indicative, buy or sell
    rate: Decimal  # rupees for one unit of the currency


@dataclass
class _Section:
    title: str
    headers: list[str]
    rows: list[list[str]]


class _ResultPage(HTMLParser):
    """Collects each '<h2>currency</h2> + <table>' pair on the page."""

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[_Section] = []
        self._capture: str | None = None  # the tag whose text we are collecting
        self._text: list[str] = []
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2" and (dict(attrs).get("id") or "").startswith("LOOKUPS"):
            self._capture = "h2"
        elif tag in ("th", "td") and self.sections:
            self._capture = tag
        elif tag == "tr":
            self._row = []
        if self._capture == tag:
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._capture:
            text = normalise_name("".join(self._text))
            if tag == "h2":
                self.sections.append(_Section(text, [], []))
            elif tag == "th":
                self.sections[-1].headers.append(text)
            elif self._row is not None:
                self._row.append(text)
            self._capture = None
        elif tag == "tr" and self._row:
            self.sections[-1].rows.append(self._row)
            self._row = None


# Indicative tables name the currency in the header, e.g. "1 USD -> LKR".
_INDICATIVE_HEADER = re.compile(r"^(\d+)\s+([A-Z]{3})\s*->\s*LKR$")


def _number(text: str) -> Decimal | None:
    cleaned = text.replace(",", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation as error:
        raise ParseError(f"not a number: {text!r}") from error
    # CBSL rounds to 4 decimals, so a very weak currency (the Iranian rial)
    # shows as 0.0000. That is a lost value, not a rate, so we treat it as missing.
    return value if value != 0 else None


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as error:
        raise ParseError(f"not a date: {text!r}") from error


def parse_rates(html: str, series: str, currencies: dict[str, str]) -> list[Rate]:
    """Read every rate on a result page.

    `currencies` is the {name: code} map from the form. The buy/sell page only
    shows currency names, so we need it to get back to codes.
    """
    page = _ResultPage()
    page.feed(html)
    rates: list[Rate] = []
    for section in page.sections:
        if series == "indicative":
            rates += _indicative(section)
        elif series == "tt":
            rates += _buy_sell(section, currencies)
        else:
            raise ParseError(f"unknown series: {series}")
    return rates


def _indicative(section: _Section) -> list[Rate]:
    match = _INDICATIVE_HEADER.match(section.headers[1]) if len(section.headers) >= 2 else None
    if not match:
        raise ParseError(f"unexpected indicative headers for {section.title}: {section.headers}")
    units, code = Decimal(match.group(1)), match.group(2)
    rates = []
    for row in section.rows:
        value = _number(row[1])
        if value is not None:
            rates.append(Rate(_date(row[0]), code, "indicative", value / units))
    return rates


def _buy_sell(section: _Section, currencies: dict[str, str]) -> list[Rate]:
    code = currencies.get(section.title)
    if code is None:
        raise ParseError(f"unknown currency name on buy/sell page: {section.title!r}")
    headers = [h.lower() for h in section.headers]
    if len(headers) != 3 or "buy" not in headers[1] or "sell" not in headers[2]:
        raise ParseError(f"unexpected buy/sell headers for {section.title}: {section.headers}")
    rates = []
    for row in section.rows:
        day = _date(row[0])
        for rate_type, cell in (("buy", row[1]), ("sell", row[2])):
            value = _number(cell)
            if value is not None:
                rates.append(Rate(day, code, rate_type, value))
    return rates
