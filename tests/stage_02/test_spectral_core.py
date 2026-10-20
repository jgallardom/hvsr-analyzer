"""Stage 2: preprocessing order, windows, FFT, Konno-Ohmachi, valid mask, guards, QC, Standard."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import signal

from hvsr_core.config import AnalysisConfig, BandpassFilter
from hvsr_core.core import run_core
from hvsr_core.curves import geometric_mean_curve
from hvsr_core.metadata import evaluate_instrument
from hvsr_core.model import ChannelMeta, InputError, Recording, RecordingDiagnostics, Segment
from hvsr_core.preprocess import analyzed_bounds, prepare
from hvsr_core.qc import screen_windows
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_core.selection import earliest_first_nonoverlap, standard_reference
from hvsr_core.spectra import (
    build_grid,
    combine_horizontal,
    compute_spectra,
    fft_frequencies,
    guarded_ratio,
    ko_dense_reference,
    ko_operator,
    window_amplitudes,
)
from hvsr_core.windows import build_candidates, overlaps, sample_counts, window_id


def _cfg(**over) -> AnalysisConfig:
    return AnalysisConfig.model_validate(over)


def _two_segment_recording(fs=20.0, n1=4000, gap=1000, n2=3000, seed=0) -> Recording:
    rng = np.random.default_rng(seed)
    segs = [
        Segment(0, 0.0, rng.standard_normal((3, n1))),
        Segment(n1 + gap, (n1 + gap) / fs, rng.standard_normal((3, n2))),
    ]
    chans = tuple(ChannelMeta(c, c, quantity="counts", units="counts") for c in "NEZ")
    return Recording(
        fs, 0, "declared", chans, segs, "declared", "float64", RecordingDiagnostics(), {"format": "test"}
    )


def _prepared(rec, cfg):
    inst = evaluate_instrument(rec, None, "none")
    return prepare(rec, cfg, inst), inst


@pytest.mark.acceptance("AC-S2-01")
def test_windows_are_half_open_integer_intervals_within_segments():
    rec = _two_segment_recording()
    cfg = _cfg(windowing={"window_length_s": 50, "step_s": 5})
    prep, _ = _prepared(rec, cfg)
    n, step = sample_counts(prep.fs, 50, 5)
    assert (n, step) == (1000, 100)
    win = build_candidates(prep, n, step)
    seg_bounds = [(s.global_start, s.global_start + s.data.shape[1]) for s in prep.segments]
    for gs, pos in zip(win.global_start, win.segment_pos, strict=True):
        a, b = seg_bounds[pos]
        assert a <= gs and gs + n <= b  # never crosses the gap
        assert (gs - prep.analyzed_start_index) % step == 0  # on the integer step grid
    assert win.global_start.dtype == np.int64
    # time bounds derive from integer indices, not accumulated floats
    t = win.start_time_s(prep.fs)
    np.testing.assert_array_equal(t, win.global_start / 20.0)
    assert np.all(win.global_stop - win.global_start == n)
    # half-open: windows that touch end-to-start share no sample
    assert not overlaps(0, 1000, 1000) and overlaps(0, 999, 1000)
    # IDs come from the global start index and are stable
    assert win.ids()[0] == window_id(int(win.global_start[0])) == "w0000000000"
    assert len(set(win.ids())) == win.count
    # second segment starts at 5000 -> first window there is at 5000 (on grid), last ends <= 8000
    second = win.global_start[win.segment_pos == 1]
    assert second[0] == 5000 and second[-1] + n <= 8000


@pytest.mark.acceptance("AC-S2-19")
def test_time_range_maps_to_integer_sample_bounds_and_raw_bounds_are_preserved():
    rec = _two_segment_recording()
    cfg = _cfg(windowing={"time_range": {"tmin_s": 10.03, "tmax_s": 300.01}})
    start, stop = analyzed_bounds(rec, cfg)
    assert (start, stop) == (math.ceil(10.03 * 20), math.floor(300.01 * 20)) == (201, 6000)
    prep, _ = _prepared(rec, cfg)
    assert prep.analyzed_start_index == 201 and prep.analyzed_stop_index == 6000
    # second segment [5000, 8000) is cut at 6000; raw recording untouched
    assert prep.segments[-1].global_start == 5000 and prep.segments[-1].data.shape[1] == 1000
    assert rec.segments[1].n == 3000 and rec.total_samples == 7000
    # window grid anchored at the analyzed start
    win = build_candidates(prep, 1000, 100)
    assert win.global_start[0] == 201
    with pytest.raises(InputError, match="no samples"):
        analyzed_bounds(rec, _cfg(windowing={"time_range": {"tmin_s": 500.0, "tmax_s": 500.01}}))


@pytest.mark.acceptance("AC-S2-02")
def test_fft_resolution_separate_from_grid_and_coverage_validated():
    rec, _ = generate_3c(duration_s=300, fs=50, tf=TFSpec("flat"), seed=5)
    cfg = AnalysisConfig()
    run = run_core(rec, cfg)
    sd = run.spectra
    assert sd.df_hz == pytest.approx(1 / 50)  # actual FFT resolution = 1/Lw, no zero padding
    assert sd.n_fft == 2500
    ratio = sd.grid.freq[1:] / sd.grid.freq[:-1]
    np.testing.assert_allclose(ratio, 10 ** (1 / 200), rtol=1e-12)  # grid spacing is separate
    np.testing.assert_allclose(sd.grid.freq, 10.0 ** (sd.grid.index / 200), rtol=0)
    reasons = sd.grid.reason_counts()
    assert reasons["insufficient_smoothing_bins"] > 0  # lowest grid points lack FFT support
    assert reasons["above_anti_alias_limit"] > 0
    assert sd.grid.freq.max() <= 25.0
    valid_f = sd.grid.freq[sd.grid.valid]
    assert valid_f.max() * 10 ** (3 / 40) <= 0.8 * 25.0
    # Nyquist validation for filters
    rec2, _ = generate_3c(duration_s=120, fs=20, tf=TFSpec("flat"), seed=6)
    with pytest.raises(InputError, match="Nyquist"):
        run_core(rec2, _cfg(preprocessing={"bandpass": {"low_hz": 0.1, "high_hz": 10.0}}))
    # sampling-rate warning when the peak band reaches 10/Lw
    assert any("10/Lw" in w for w in run.warnings)


@pytest.mark.acceptance("AC-S2-03")
def test_sparse_konno_ohmachi_equals_direct_reference():
    rng = np.random.default_rng(3)
    n, fs = 2500, 50.0
    f = fft_frequencies(n, fs)
    amp = np.abs(rng.standard_normal(f.size)) + 0.1
    idx = np.arange(-120, 270)
    op, counts, eff = ko_operator(n, fs, idx, 200, 40.0, 3.0)
    sm = op @ amp
    fc = 10.0 ** (idx / 200)
    for j in range(0, idx.size, 7):
        ref = ko_dense_reference(f, amp, fc[j], 40.0, 3.0)
        if counts[j] == 0:
            assert math.isnan(ref)
            continue
        assert sm[j] == pytest.approx(ref, rel=1e-12)
    # normalization: rows sum to one where support exists
    rows = np.asarray(op.sum(axis=1)).ravel()
    np.testing.assert_allclose(rows[counts > 0], 1.0, rtol=1e-12)
    # support: nonzero weights only within |b log10(f/fc)| <= 3, never DC
    coo = op.tocoo()
    x = 40.0 * np.log10(f[coo.col] / fc[coo.row])
    assert np.all(np.abs(x) <= 3.0 + 1e-12) and np.all(coo.col > 0)
    assert np.all(eff[counts > 0] <= counts[counts > 0] + 1e-9)


@pytest.mark.acceptance("AC-S2-12")
def test_effective_valid_mask_covers_nyquist_smoothing_and_usable_band():
    rec, _ = generate_3c(duration_s=300, fs=50, tf=TFSpec("flat"), seed=7)
    cfg = AnalysisConfig()
    grid = build_grid(cfg, 50.0, 2500, [0.5, 10.0])
    reasons = dict(zip(grid.freq.round(6), grid.invalid_reason, strict=True))
    for f, r in reasons.items():
        top = f * 10 ** (3 / 40)
        if top > 0.8 * 25.0:
            assert r == "above_anti_alias_limit"
        elif r != "insufficient_smoothing_bins":
            assert (r == "outside_declared_usable_band") == (f < 0.5 or f > 10.0)
    assert grid.valid.sum() == sum(r is None for r in grid.invalid_reason)
    assert np.all((grid.freq[grid.valid] >= 0.5) & (grid.freq[grid.valid] <= 10.0))
    # windows inherit the grid mask; invalid bins are NaN, never zero
    inst = evaluate_instrument(rec, None, "none")
    inst["usable_band_hz"] = [0.5, 10.0]
    run = run_core(rec, cfg, instrument=inst)
    assert not run.spectra.valid[:, ~run.grid.valid].any()
    assert np.all(np.isnan(run.spectra.hv[:, ~run.grid.valid]))


@pytest.mark.acceptance("AC-S2-11")
def test_zero_and_invalid_vertical_bins_are_masked_not_zero():
    H = np.array([[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]])
    V = np.array([[1.0, 0.0, -1.0, np.nan, 1e-14, 2.0]])
    hv, valid, counts = guarded_ratio(H, V, np.ones(6, dtype=bool), 1e-9)
    assert valid.tolist() == [[True, False, False, False, False, True]]
    assert hv[0, 0] == 1.0 and hv[0, 5] == 3.0
    assert np.all(np.isnan(hv[0, 1:5]))  # not zero, not inf, not a substituted floor
    assert counts["vertical_below_floor"] == 4
    # H <= 0 also invalid
    hv2, valid2, _ = guarded_ratio(
        np.array([[0.0, 1.0]]), np.array([[1.0, 1.0]]), np.ones(2, dtype=bool), 1e-9
    )
    assert valid2.tolist() == [[False, True]] and math.isnan(hv2[0, 0])
    # integration: a window whose vertical is all zeros is invalid everywhere
    rec, _ = generate_3c(duration_s=400, fs=50, tf=TFSpec("flat"), seed=8)
    rec.segments[0].data[2, 0:5000] = 0.0
    cfg = AnalysisConfig()
    inst = evaluate_instrument(rec, None, "none")
    prep = prepare(rec, cfg, inst)
    win = build_candidates(prep, 2500, 250)
    grid = build_grid(cfg, 50.0, 2500, [None, None])
    sd = compute_spectra(prep, win, grid, cfg)
    dead = win.global_start + 2500 <= 5000
    assert dead.any()
    assert not sd.valid[dead].any() and np.all(np.isnan(sd.hv[dead]))
    assert sd.invalid_counts["vertical_below_floor"] > 0
    # basic QC rejects those windows as dead_channel; the mean ignores invalid bins rather than zeroing them
    qc = screen_windows(rec, prep, win, cfg)
    assert all("dead_channel" in qc.reasons[k] for k in np.flatnonzero(dead))
    mixed = np.array([int(np.flatnonzero(dead)[0]), int(np.flatnonzero(~dead)[-1])])
    mc = geometric_mean_curve(sd, mixed)
    ok = grid.valid
    assert np.all(mc.count[ok] == 1)
    np.testing.assert_allclose(mc.hv[ok], sd.hv[mixed[1], ok], rtol=1e-12)


@pytest.mark.acceptance("AC-S2-14")
def test_rms_horizontal_default_and_geometric_explicit():
    assert AnalysisConfig().spectral.horizontal_combination == "rms"
    n = np.array([3.0, 1.0])
    e = np.array([4.0, 1.0])
    np.testing.assert_allclose(combine_horizontal(n, e, "rms"), np.sqrt((n**2 + e**2) / 2))
    np.testing.assert_allclose(combine_horizontal(n, e, "geometric"), np.sqrt(n * e))
    rec, _ = generate_3c(duration_s=300, fs=50, tf=TFSpec("flat"), seed=9, horizontal_gain_mismatch=3.0)
    r_rms = run_core(rec, AnalysisConfig())
    r_geo = run_core(rec, _cfg(spectral={"horizontal_combination": "geometric"}))
    # combination is applied to raw Fourier amplitudes, then smoothed (combine_then_smooth)
    k = np.array([2])
    amps = window_amplitudes(r_rms.prep, r_rms.window, k, r_rms.config)
    op, _, _ = ko_operator(2500, 50.0, r_rms.grid.index, 200, 40.0, 3.0)
    ok = r_rms.grid.valid
    np.testing.assert_allclose(
        r_rms.spectra.H[2, ok], (op @ np.sqrt((amps[0, 0] ** 2 + amps[1, 0] ** 2) / 2))[ok], rtol=1e-12
    )
    np.testing.assert_allclose(
        r_geo.spectra.H[2, ok], (op @ np.sqrt(amps[0, 0] * amps[1, 0]))[ok], rtol=1e-12
    )
    np.testing.assert_array_equal(r_rms.spectra.V, r_geo.spectra.V)
    # RMS >= geometric (power-mean inequality) and they differ under gain mismatch
    assert np.all(r_rms.spectra.H[:, ok] >= r_geo.spectra.H[:, ok])
    assert np.nanmedian(r_rms.spectra.hv[:, ok] / r_geo.spectra.hv[:, ok]) > 1.2
    assert r_rms.config.scientific_hash() != r_geo.config.scientific_hash()


@pytest.mark.acceptance("AC-S2-15")
def test_preprocessing_order_fixed_filter_off_and_operations_recorded():
    rec, _ = generate_3c(duration_s=300, fs=50, tf=TFSpec("flat"), seed=10)
    cfg = AnalysisConfig()
    assert cfg.preprocessing.bandpass is None
    prep, inst = _prepared(rec, cfg)
    assert prep.derived_operations == []
    np.testing.assert_array_equal(prep.segments[0].data, rec.segments[0].data)
    # per-window: demean -> linear detrend -> Tukey(0.1) -> rFFT / fs
    win = build_candidates(prep, 2500, 250)
    amps = window_amplitudes(prep, win, np.array([3]), cfg)
    x = rec.segments[0].data[:, 750:3250]
    x = x - x.mean(axis=1, keepdims=True)
    x = signal.detrend(x, axis=1, type="linear")
    manual = np.abs(np.fft.rfft(x * signal.windows.tukey(2500, 0.1), axis=1)) / 50.0
    np.testing.assert_allclose(amps[:, 0, :], manual, rtol=1e-12, atol=0)
    # filter on: recorded as a derived operation with exact parameters
    cfg_f = _cfg(preprocessing={"bandpass": BandpassFilter(low_hz=0.1, high_hz=15.0).model_dump()})
    prep_f, _ = _prepared(rec, cfg_f)
    ops = prep_f.derived_operations
    assert ops[0]["operation"] == "butterworth_filter"
    assert (
        ops[0]["parameters"]["low_hz"] == 0.1
        and ops[0]["parameters"]["implementation"] == "scipy.signal.sosfiltfilt"
    )
    assert not np.array_equal(prep_f.segments[0].data, rec.segments[0].data)
    # scalar sensitivity correction recorded as an operation
    inst2 = dict(inst, correction="scalar_sensitivity", scale={"N": 0.5, "E": 0.5, "Z": 0.5})
    prep_s = prepare(rec, cfg, inst2)
    assert prep_s.derived_operations[0]["operation"] == "scalar_sensitivity_correction"
    np.testing.assert_allclose(prep_s.segments[0].data, 0.5 * rec.segments[0].data)


@pytest.mark.acceptance("AC-S2-10")
def test_clipped_windows_detected_and_excluded():
    rec, _ = generate_3c(duration_s=600, fs=50, tf=TFSpec("flat"), seed=11)
    d = rec.segments[0].data
    fs_val = float(np.abs(d).max() * 1.5)
    chans = tuple(
        ChannelMeta(c.component, c.source_label, quantity="counts", units="counts", full_scale=fs_val)
        for c in rec.channels
    )
    rec = Recording(
        rec.sampling_rate_hz,
        rec.t0_utc_ns,
        rec.t0_source,
        chans,
        rec.segments,
        "declared",
        "float64",
        RecordingDiagnostics(),
        rec.source,
    )
    d[0, 10000:10003] = fs_val  # declared full scale reached at 200 s
    d[1, 20000:20010] = d[1].max()  # flat-topped run at 400 s (no full scale needed)
    run = run_core(rec, AnalysisConfig())
    win = run.window
    for k in range(win.count):
        s, e = int(win.global_start[k]), int(win.global_stop[k])
        hit_a = s < 10003 and 10000 < e
        hit_b = s < 20010 and 20000 < e
        assert ("clipped" in run.qc.reasons[k]) == (hit_a or hit_b)
    clipped_rows = {k for k in range(win.count) if "clipped" in run.qc.reasons[k]}
    assert clipped_rows and not clipped_rows & set(run.standard.rows.tolist())
    assert not clipped_rows & set(run.discovery.rows.tolist())


@pytest.mark.acceptance("AC-S2-18")
def test_standard_reference_is_deterministic_earliest_first_and_fdwra_separate():
    rec, _ = generate_3c(
        duration_s=1500,
        fs=50,
        tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}),
        seed=12,
        transients=[{"time_s": 333.0, "amplitude_rel": 20.0, "duration_s": 1.0}],
        regimes=[{"start_s": 1000.0, "end_s": 1150.0, "tf": TFSpec("sdof", {"fn": 6.0, "zeta": 0.1})}],
    )
    cfg = AnalysisConfig()
    a = run_core(rec, cfg)
    b = run_core(rec, cfg)
    np.testing.assert_array_equal(a.standard.rows, b.standard.rows)
    rows = a.standard.rows
    np.testing.assert_array_equal(rows, earliest_first_nonoverlap(a.window, a.qc.passed))
    st = a.window.global_start[rows]
    assert np.all(np.diff(st) >= a.window.n_samples)
    assert all(a.qc.passed[rows])
    # earliest-first: every skipped passing candidate overlaps a previously chosen window
    chosen = set(rows.tolist())
    for k in range(a.window.count):
        if a.qc.passed[k] and k not in chosen:
            prior = [r for r in rows if a.window.global_start[r] <= a.window.global_start[k]]
            assert any(
                overlaps(int(a.window.global_start[r]), int(a.window.global_start[k]), a.window.n_samples)
                for r in prior
            )
    assert a.standard.fdwra is None and np.array_equal(a.standard.basic_rows, rows)
    # transient window rejected by STA/LTA
    t = a.window.global_start / 50.0
    hit = (t <= 333.0) & (t + 50 > 333.0)
    assert any("sta_lta_out_of_range" in a.qc.reasons[k] for k in np.flatnonzero(hit))
    # optional FDWRA: separate step with a basic-only audit reference
    cfg_f = _cfg(standard_selection={"frequency_outlier_rejection": "fdwra_cox2020"})
    f = run_core(rec, cfg_f)
    np.testing.assert_array_equal(f.standard.basic_rows, rows)
    assert set(f.standard.rows.tolist()) < set(rows.tolist())
    rej_t = f.window.global_start[f.standard.fdwra.rejected] / 50.0
    assert np.any((rej_t >= 950) & (rej_t < 1150))  # regime-switch windows are frequency outliers
    desc = f.standard.describe(f.window)
    assert desc["frequency_outlier_rejection"] == "fdwra_cox2020" and desc["fdwra"]["converged"]
    sr = standard_reference(f.spectra, f.qc.passed, cfg_f)
    np.testing.assert_array_equal(sr.rows, f.standard.rows)
