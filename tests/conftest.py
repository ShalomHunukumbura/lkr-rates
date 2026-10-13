import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page():
    """Load a saved CBSL page and the currency map that came with it."""

    def load(series: str) -> tuple[str, dict[str, str]]:
        html = (FIXTURES / f"{series}.html").read_text()
        meta = json.loads((FIXTURES / f"{series}.meta.json").read_text())
        return html, meta["currencies"]

    return load
