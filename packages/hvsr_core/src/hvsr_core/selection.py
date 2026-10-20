"""Shape-neutral Standard reference (std-earliest-nonoverlap-v1) and optional FDWRA (fdwra-cox2020-impl-v1).

The SDOF greedy selector lives in hvsr_core.matching (Stage 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import AnalysisConfig
from .spectra import SpectralData
from .windows import WindowTable


def earliest_first_nonoverlap(win: WindowTable, eligible: np.ndarray) -> np.ndarray:
    """Rows of eligible windows chosen earliest-first so no two share a sample."""
    order = np.lexsort((np.arange(win.count), win.global_start))
    chosen = []
    last_stop = None
    for r in order:
        if not eligible[r]:
            continue
        s = int(win.global_start[r])
        if last_stop is None or s >= last_stop:
            chosen.append(int(r))
            last_stop = s + win.n_samples
    return np.asarray(chosen, dtype=np.int64)


def search_band_mask(sd: SpectralData, config: AnalysisConfig) -> np.ndarray:
    f = sd.grid.freq
    return (f >= config.peak_search.fmin_hz) & (f <= config.peak_search.fmax_hz) & sd.grid.valid


def window_max_frequency(sd: SpectralData, rows: np.ndarray, band: np.ndarray) -> np.ndarray:
    """Frequency of the maximum H/V of each window inside the band (NaN if none valid)."""
    out = np.full(rows.size, np.nan)
    for k, r in enumerate(rows):
        y = np.where(band & sd.valid[r], sd.hv[r], np.nan)
        if np.all(np.isnan(y)):
            continue
        out[k] = sd.grid.freq[int(np.nanargmax(y))]
    return out


@dataclass
class FDWRAResult:
    kept: np.ndarray
    rejected: np.ndarray
    iterations: int
    converged: bool
    history: list[dict] = field(default_factory=list)


def fdwra(sd: SpectralData, rows: np.ndarray, config: AnalysisConfig) -> FDWRAResult:
    """Frequency-domain window rejection in the style of Cox et al. (2020).

    Iteratively keep windows whose ln(f_peak) lies within n_std standard deviations of the mean of
    the currently kept windows; stop when the kept set no longer changes. Our implementation, not a
    copy of hvsrpy's code.
    """
    sel = config.standard_selection
    band = search_band_mask(sd, config)
    fpk = window_max_frequency(sd, rows, band)
    keep = np.isfinite(fpk)
    history = []
    converged = False
    it = 0
    for it in range(1, sel.fdwra_max_iterations + 1):
        lf = np.log(fpk[keep])
        if lf.size < 2:
            break
        mu, sd_ = float(lf.mean()), float(lf.std(ddof=1))
        with np.errstate(invalid="ignore"):
            new = np.isfinite(fpk) & (np.abs(np.log(np.where(np.isfinite(fpk), fpk, 1.0)) - mu) <= sel.fdwra_n_std * sd_)
        history.append({"iteration": it, "mean_ln_f0": mu, "std_ln_f0": sd_, "kept": int(new.sum())})
        if np.array_equal(new, keep):
            converged = True
            break
        keep = new
    return FDWRAResult(rows[keep], rows[~keep], it, converged, history)


@dataclass
class StandardResult:
    rows: np.ndarray
    basic_rows: np.ndarray
    fdwra: FDWRAResult | None

    def describe(self, win: WindowTable) -> dict:
        ids = win.ids()
        out = {
            "rule": "basic_qc_then_earliest_first_nonoverlap",
            "window_ids": [ids[r] for r in self.rows],
            "frequency_outlier_rejection": "none" if self.fdwra is None else "fdwra_cox2020",
        }
        if self.fdwra is not None:
            out["fdwra"] = {
                "iterations": self.fdwra.iterations,
                "converged": self.fdwra.converged,
                "rejected_window_ids": [ids[r] for r in self.fdwra.rejected],
                "history": self.fdwra.history,
            }
        return out


def standard_reference(sd: SpectralData, qc_passed: np.ndarray, config: AnalysisConfig) -> StandardResult:
    basic = earliest_first_nonoverlap(sd.window, qc_passed)
    if config.standard_selection.frequency_outlier_rejection == "fdwra_cox2020":
        fr = fdwra(sd, basic, config)
        return StandardResult(fr.kept, basic, fr)
    return StandardResult(basic, basic, None)
