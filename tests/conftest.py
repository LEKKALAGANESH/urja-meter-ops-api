"""Shared test fixtures.

Payload fixtures are **recorded from the live portal**, not hand-written. Invented
fixtures only prove that the code agrees with the author's assumptions; recorded ones
prove it agrees with the system it has to talk to. Each file under ``tests/fixtures/``
is a verbatim capture - see ``PROTOCOL.md`` for how each was obtained.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def export_rows() -> list[dict]:
    """Bulk-export records covering clean, blank-code, blank-name and DT-alias cases."""
    return load_fixture("export_sample.json")["data"]


@pytest.fixture
def dt_rows() -> list[dict]:
    return load_fixture("dts_page1.json")["data"]


@pytest.fixture
def energy_30min() -> list[dict]:
    """A 30-minute-cadence series (the common case)."""
    return load_fixture("energy_30min.json")["data"]


@pytest.fixture
def energy_daily() -> list[dict]:
    """A daily-cadence series - proves cadence must be inferred, not assumed."""
    return load_fixture("energy_daily.json")["data"]


@pytest.fixture
def ssr_legacy() -> dict:
    """``__data.json`` for a ``build=legacy`` meter (parameterName/parameterValue rows)."""
    return load_fixture("ssr_detail_legacy.json")


@pytest.fixture
def ssr_v2() -> dict:
    """``__data.json`` for a ``build=v2`` meter (double-encoded ``classData``)."""
    return load_fixture("ssr_detail_v2.json")
