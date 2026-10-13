"""The raw layer: every page we download, saved exactly as it arrived.

Raw files are never edited and never deleted. If the parser turns out to be
wrong, we fix the parser and rebuild the clean table from these files, without
asking CBSL for anything again.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class RawFile:
    series: str
    start: date
    end: date
    fetched_at: datetime
    currencies: dict[str, str]
    path: Path

    def html(self) -> str:
        with gzip.open(self.path, "rt", encoding="utf-8") as handle:
            return handle.read()


def raw_dir(root: Path) -> Path:
    return root / "data" / "raw"


def save_raw(
    root: Path,
    series: str,
    start: date,
    end: date,
    html: str,
    currencies: dict[str, str],
    fetched_at: datetime,
) -> RawFile:
    """Write the page and a small note about how we got it."""
    folder = raw_dir(root) / series
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{start.isoformat()}_{end.isoformat()}_{fetched_at:%Y%m%dT%H%M%SZ}"
    path = folder / f"{stem}.html.gz"
    # mtime=0 keeps the gzip header free of the current time.
    with open(path, "wb") as handle, gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as zipped:
        zipped.write(html.encode("utf-8"))
    meta = {
        "series": series,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "currencies": currencies,
    }
    (folder / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return RawFile(series, start, end, fetched_at, currencies, path)


def iter_raw(root: Path) -> Iterator[RawFile]:
    """Every raw file, oldest download first."""
    files = []
    for meta_path in raw_dir(root).glob("*/*.meta.json"):
        meta = json.loads(meta_path.read_text())
        files.append(
            RawFile(
                series=meta["series"],
                start=date.fromisoformat(meta["start"]),
                end=date.fromisoformat(meta["end"]),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
                currencies=meta["currencies"],
                path=meta_path.with_name(meta_path.name.replace(".meta.json", ".html.gz")),
            )
        )
    yield from sorted(files, key=lambda f: (f.fetched_at, f.series, f.start))


def has_range(root: Path, series: str, start: date, end: date) -> bool:
    """True if this exact range was already downloaded for this series."""
    return any((raw_dir(root) / series).glob(f"{start.isoformat()}_{end.isoformat()}_*.meta.json"))
