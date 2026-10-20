"""Benchmark of D-042: conditioned templates vs. the processing they approximate.

The pipeline smooths H and V separately on discrete FFT bins and then divides. The conditioned bank
smooths the theoretical ratio with the continuous-limit Konno-Ohmachi window. For a flat base spectrum
the two agree up to discretization; for a colored base they differ. This test computes the R2_log of
the noise-free processed ratio against raw and conditioned banks and writes the numbers to
artifacts/benchmarks/template_conditioning.json (summarized in docs/template-banks.md).
"""

from __future__ import annotations

import json

import numpy as np

from hvsr_core.config import AnalysisConfig
from hvsr_core.matching import match_family
from hvsr_core.spectra import fft_frequencies, ko_operator
from hvsr_core.templates import DEFAULT_SDOF_DAMPING, get_bank, sdof_m

FS, N = 50.0, 2500  # Lw = 50 s
IDX = np.arange(-160, 300)
GRID = 10.0 ** (IDX / 200)


def _processed_ratio(fn, zeta, base_alpha):
    f = fft_frequencies(N, FS)
    base = np.where(f > 0, np.maximum(f, 1e-9) ** (-base_alpha / 2), 0.0)
    op, counts, _ = ko_operator(N, FS, IDX, 200, 40.0, 3.0)
    h = op @ (base * sdof_m(f / fn, zeta))
    v = op @ base
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.log(h / v)
    return out, counts >= 3


def test_conditioned_bank_benchmark(repo_root):
    cfg = AnalysisConfig()
    raw, ko = get_bank("sdof-raw-v1"), get_bank("sdof-ko40-v1")
    results = []
    for alpha in (0.0, 2.0):
        for fn in (0.6, 2.0, 8.0):
            for k, z in enumerate(DEFAULT_SDOF_DAMPING):
                ln, ok = _processed_ratio(fn, z, alpha)
                fp_guess = fn * raw.anchor_ratio[k]
                lo, hi = (
                    int(np.searchsorted(GRID, fp_guess / 1.15)),
                    int(np.searchsorted(GRID, fp_guess * 1.15)),
                )
                row = {"base_alpha": alpha, "fn_hz": fn, "zeta": z}
                for name, bank in (("raw", raw), ("ko40", ko)):
                    t = match_family(
                        ln[None, :], ok[None, :], np.log(GRID), FS / N, np.arange(1), "F1", lo, hi, bank, cfg
                    )
                    row[f"{name}_status"] = t.status[0]
                    row[f"{name}_best_r2"] = None if np.isnan(t.best_r2[0]) else float(t.best_r2[0])
                    row[f"{name}_best_zeta"] = (
                        None if t.best_k[0] < 0 else float(bank.param_values[t.best_k[0], 0])
                    )
                results.append(row)
    out = repo_root / "artifacts" / "benchmarks"
    out.mkdir(parents=True, exist_ok=True)
    (out / "template_conditioning.json").write_text(
        json.dumps({"description": __doc__.strip(), "results": results}, indent=1)
    )
    rated = [r for r in results if r["raw_best_r2"] is not None and r["ko40_best_r2"] is not None]
    assert len(rated) >= 0.8 * len(results)
    flat = [r for r in rated if r["base_alpha"] == 0.0]
    # flat base: conditioned bank reproduces the processed ratio closely and recovers zeta
    assert np.median([1 - r["ko40_best_r2"] for r in flat]) < 1e-3
    assert np.mean([r["ko40_best_zeta"] == r["zeta"] for r in flat]) >= 0.7
    # conditioning is never worse than raw on median over cases
    assert np.median([r["ko40_best_r2"] - r["raw_best_r2"] for r in rated]) >= 0
