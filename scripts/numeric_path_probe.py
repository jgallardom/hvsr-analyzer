"""Does this machine's numerical code path reproduce the byte-exact references?

Regenerates the shipped template banks and the docs/examples exports, compares them with the
committed files, and prints one JSON line: the NumPy SIMD targets and OpenBLAS kernels in use, which
files are byte-identical, the largest ULP and relative differences, and whether any analysis value
falls outside TOLERANCE_POLICY. Run it natively and with the variables in tests/conftest.py
REFERENCE_NUMERIC_PATH set to compare the two (D-100, docs/evidence/stage9/numeric-path-probe.json).

    uv run python scripts/numeric_path_probe.py [label]
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from numpy._core._multiarray_umath import __cpu_dispatch__, __cpu_features__
from threadpoolctl import threadpool_info

ROOT = Path(__file__).resolve().parents[1]


def cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def max_ulp(a: np.ndarray, b: np.ndarray) -> int:
    a, b = np.asarray(a, dtype=np.float64).ravel(), np.asarray(b, dtype=np.float64).ravel()
    ok = np.isfinite(a) & np.isfinite(b) & (np.sign(a) == np.sign(b))
    if not ok.any():
        return 0
    return int(np.max(np.abs(a[ok].view(np.int64) - b[ok].view(np.int64))))


def max_rel(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, dtype=np.float64).ravel(), np.asarray(b, dtype=np.float64).ravel()
    ok = np.isfinite(a) & np.isfinite(b)
    if not ok.any():
        return 0.0
    den = np.maximum(np.abs(a[ok]), np.abs(b[ok]))
    den[den == 0] = 1.0
    return float(np.max(np.abs(a[ok] - b[ok]) / den))


def banks() -> list[dict]:
    from hvsr_core.mdof import generate_mdof_bank
    from hvsr_core.templates import (
        RAW,
        SHIPPED_DIR,
        SHIPPED_KO_BANDWIDTHS,
        generate_sdof_bank,
        ko_conditioning,
    )

    regenerated = [generate_sdof_bank(RAW)] + [
        generate_sdof_bank(ko_conditioning(b)) for b in SHIPPED_KO_BANDWIDTHS
    ]
    regenerated.append(
        generate_mdof_bank(
            2, RAW, (0.25, 0.5, 1.0, 2.0), (0.25, 0.5, 1.0, 2.0), (0.02, 0.05, 0.1, 0.2), "mdof2-raw-v1"
        )
    )
    regenerated.append(
        generate_mdof_bank(3, RAW, (0.5, 1.0, 2.0), (0.5, 1.0, 2.0), (0.02, 0.05, 0.1, 0.2), "mdof3-raw-v1")
    )
    out = []
    for manifest, data in regenerated:
        shipped_m = json.loads((SHIPPED_DIR / f"{manifest['bank_id']}.json").read_text())
        new = np.load(io.BytesIO(data))
        old = np.load(SHIPPED_DIR / shipped_m["data_file"])
        out.append(
            {
                "bank": manifest["bank_id"],
                "bytes_equal": manifest["data_sha256"] == shipped_m["data_sha256"],
                "max_rel": max(max_rel(new[k], old[k]) for k in old.files),
                "max_ulp": max(max_ulp(new[k], old[k]) for k in old.files),
                "max_abs": max(float(np.nanmax(np.abs(new[k] - old[k]))) for k in old.files),
            }
        )
    return out


def compare_csv(new: Path, old: Path) -> dict:
    a = list(csv.reader(new.read_text().splitlines()))
    b = list(csv.reader(old.read_text().splitlines()))
    worst, ulp, text_diffs = 0.0, 0, 0
    if len(a) != len(b):
        return {"bytes_equal": False, "rows": [len(a), len(b)]}
    for ra, rb in zip(a, b, strict=True):
        for x, y in zip(ra, rb, strict=True):
            if x == y:
                continue
            try:
                fx, fy = float(x), float(y)
            except ValueError:
                text_diffs += 1
                continue
            worst = max(worst, max_rel(np.array([fx]), np.array([fy])))
            ulp = max(ulp, max_ulp(np.array([fx]), np.array([fy])))
    return {
        "bytes_equal": new.read_bytes() == old.read_bytes(),
        "max_rel": worst,
        "max_ulp": ulp,
        "non_numeric_cells_differing": text_diffs,
    }


def examples() -> dict:
    from hvsr_core.analysis import TOLERANCE_POLICY, _walk_compare

    spec = importlib.util.spec_from_file_location("export_examples", ROOT / "scripts/export_examples.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    with tempfile.TemporaryDirectory() as tmp:
        exporter.export(Path(tmp))
        out = {}
        for p in sorted(Path(tmp).glob("*.csv")):
            out[p.name] = compare_csv(p, ROOT / "docs/examples" / p.name)
        for name in ("analysis_sdof.json", "analysis_standard.json"):
            new = _science(json.loads((Path(tmp) / name).read_text()))
            old = _science(json.loads((ROOT / "docs/examples" / name).read_text()))
            policy, exact = [], []
            _walk_compare(
                new, old, "$", policy, TOLERANCE_POLICY["float_rtol"], TOLERANCE_POLICY["float_atol"]
            )
            _walk_compare(new, old, "$", exact, 0.0, 0.0)
            fa, fb = _flat(new), _flat(old)
            common = sorted(set(fa) & set(fb))
            a = np.array([fa[k] for k in common], dtype=float)
            b = np.array([fb[k] for k in common], dtype=float)
            out[name] = {
                "n_beyond_tolerance_policy": len(policy),
                "beyond_tolerance_policy": policy[:5],
                "n_exact_differences": len(exact),
                "non_numeric_differences": [d for d in exact if d["issue"] != "numeric difference"][:5],
                "numbers_compared": len(common),
                "max_rel": max_rel(a, b),
                "max_ulp": max_ulp(a, b),
            }
        return out


def _science(doc: dict) -> dict:
    for k in ("provenance", "licensing", "record", "run_id"):
        doc.pop(k, None)
    doc.get("windows", {}).pop("candidates_url", None)
    return doc


def _flat(x, path: str = "$", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            _flat(v, f"{path}.{k}", out)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            _flat(v, f"{path}[{i}]", out)
    elif isinstance(x, float):
        out[path] = x
    return out


def main() -> None:
    report = {
        "label": sys.argv[1] if len(sys.argv) > 1 else "",
        "cpu": cpu_model(),
        "numpy": np.__version__,
        "numpy_simd_enabled": [k for k in __cpu_dispatch__ if __cpu_features__.get(k)],
        "NPY_DISABLE_CPU_FEATURES": os.environ.get("NPY_DISABLE_CPU_FEATURES"),
        "OPENBLAS_CORETYPE": os.environ.get("OPENBLAS_CORETYPE"),
        "blas": [
            {k: i.get(k) for k in ("internal_api", "architecture", "version")}
            for i in threadpool_info()
            if i.get("user_api") == "blas"
        ],
        "banks": banks(),
        "examples": examples(),
    }
    print("PROBE " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
