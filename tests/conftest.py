"""Shared pytest configuration.

Acceptance IDs attached with ``@pytest.mark.acceptance("AC-Sx-yy")`` are copied into the JUnit XML
as ``<property name="acceptance" value="AC-Sx-yy"/>`` so ``scripts/gate.py`` can trace them.
"""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path

_TMP = Path(tempfile.gettempdir()) / "hvsr-analyzer-test-cache"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("NUMBA_CACHE_DIR", str(_TMP / "numba"))
os.environ.setdefault("MPLCONFIGDIR", str(_TMP / "mpl"))
os.environ.setdefault("MPLBACKEND", "Agg")

# The byte-exact references (shipped banks, docs/examples, performance counts) were made on this
# numerical code path; AVX-512 paths round differently in the last bits (D-100). NumPy and OpenBLAS
# read these only when NumPy is first imported, and refuse them on CPUs without x86-64-v3.
REFERENCE_NUMERIC_PATH = {"NPY_ENABLE_CPU_FEATURES": "X86_V3", "OPENBLAS_CORETYPE": "Haswell"}
_X86_64_V3_FLAGS = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "movbe", "abm"}


def _cpu_flags() -> set[str]:
    try:
        lines = Path("/proc/cpuinfo").read_text().splitlines()
    except OSError:
        return set()
    return next((set(line.split(":", 1)[1].split()) for line in lines if line.startswith("flags")), set())


if platform.machine() == "x86_64" and _X86_64_V3_FLAGS <= _cpu_flags():
    for _name, _value in REFERENCE_NUMERIC_PATH.items():
        os.environ.setdefault(_name, _value)

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
