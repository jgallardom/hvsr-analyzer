"""Stage-2 core pipeline: preprocessing -> windows -> spectra -> basic QC -> Standard -> discovery.

Both methods (Standard and SDOF-assisted) start from the same SpectralData and the same basic QC.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import AnalysisConfig
from .metadata import evaluate_instrument
from .model import InputError, Recording
from .peaks import Discovery, discover
from .preprocess import Prepared, prepare
from .qc import WindowQC, screen_windows
from .selection import StandardResult, standard_reference
from .spectra import Grid, SpectralData, build_grid, compute_spectra
from .windows import WindowTable, build_candidates, sample_counts


@dataclass
class CoreRun:
    config: AnalysisConfig
    instrument: dict
    prep: Prepared
    window: WindowTable
    grid: Grid
    spectra: SpectralData
    qc: WindowQC
    standard: StandardResult
    discovery: Discovery
    warnings: list[str]


def run_core(
    rec: Recording,
    config: AnalysisConfig,
    instrument: dict | None = None,
    context: str = "unknown",
) -> CoreRun:
    if instrument is None:
        instrument = evaluate_instrument(rec, None, config.preprocessing.response_correction)
    if instrument["status"] == "invalid":
        raise InputError("instrument_invalid", "; ".join(instrument["reasons"]))
    prep = prepare(rec, config, instrument)
    n, step = sample_counts(prep.fs, config.windowing.window_length_s, config.windowing.step_s)
    win = build_candidates(prep, n, step)
    if win.count == 0:
        raise InputError("no_windows", f"no {config.windowing.window_length_s} s window fits inside the analyzed span")
    grid = build_grid(config, prep.fs, n, instrument.get("usable_band_hz", [None, None]))
    sd = compute_spectra(prep, win, grid, config)
    qc = screen_windows(rec, prep, win, config)
    std = standard_reference(sd, qc.passed, config)
    disc = discover(prep, sd, std.rows, config, context)
    warnings = list(prep.warnings)
    n_sl = qc.counts().get("sta_lta_out_of_range", 0)
    if win.count and n_sl > 0.5 * win.count:
        warnings.append(
            f"STA/LTA rejected {n_sl} of {win.count} candidate windows. Narrow-band or low-frequency-dominated "
            f"signals fluctuate on time scales longer than STA = {config.basic_qc.sta_s} s; consider a longer STA "
            "(several dominant periods) or reviewing the anti-trigger thresholds."
        )
    if not np.any(grid.valid):
        warnings.append("no valid analysis frequency: check window length, sampling rate and usable band")
    return CoreRun(config, instrument, prep, win, grid, sd, qc, std, disc, warnings)
