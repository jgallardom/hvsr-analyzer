"""Shared pytest configuration.

Acceptance IDs attached with ``@pytest.mark.acceptance("AC-Sx-yy")`` are copied into the JUnit XML
as ``<property name="acceptance" value="AC-Sx-yy"/>`` so ``scripts/gate.py`` can trace them.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "hvsr-analyzer-test-cache"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("NUMBA_CACHE_DIR", str(_TMP / "numba"))
os.environ.setdefault("MPLCONFIGDIR", str(_TMP / "mpl"))
os.environ.setdefault("MPLBACKEND", "Agg")

import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "acceptance(*ids): acceptance requirement IDs covered by this test")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        for marker in item.iter_markers("acceptance"):
            for acc_id in marker.args:
                item.user_properties.append(("acceptance", str(acc_id)))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR
