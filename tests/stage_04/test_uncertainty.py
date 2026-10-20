"""Stage 4: geometric mean, temporal block bootstrap, peak replicates, CI status, one-band export."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pytest

from hvsr_core.config import AnalysisConfig, Uncertainty
from hvsr_core.core import run_core
from hvsr_core.curves import geometric_mean_curve
from hvsr_core.model import Gap, Segment
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_core.selection import search_band_mask
from hvsr_core.uncertainty import (
    LABEL,
    band_from_replicates,
    export_curve,
    is_regular,
    peak_replicates,
    plan_original_time,
    plan_regular,
    replicate_counts,
    replicate_means,
    selected_mean_ci,
    selection_uncertainty,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stage_02"))
from stage2_cases import core_run  # noqa: E402

CFG = Uncertainty()


def _std_uncertainty(case: str = "sdof2", seed: int = 1, **kw):
    run, _ = core_run(case, seed)
    sd = run.spectra
    band = search_band_mask(sd, run.config)
    fam = run.discovery.plausible_families()
    su = selection_uncertainty(
        sd,
        run.prep,
        run.standard.rows,
        run.discovery.mean,
        run.config,
        band,
        fam[0] if fam else None,
        run.discovery.families,
        **kw,
    )
    return run, su, band


def _ar1(n: int, phi: float, rng: np.random.Generator) -> np.ndarray:
    x = np.empty(n)
    x[0] = rng.standard_normal() / np.sqrt(1 - phi**2)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.standard_normal()
    return x


def _synthetic_L(offsets: np.ndarray, ng: int = 40, noise: float = 0.05, seed: int = 0):
    """ln H/V per window: smooth peak profile + a per-window common offset + small bin noise."""
    rng = np.random.default_rng(seed)
    x = np.linspace(-1, 1, ng)
    profile = 1.2 * np.exp(-0.5 * (x / 0.25) ** 2)
    L = profile[None, :] + offsets[:, None] + noise * rng.standard_normal((offsets.size, ng))
    feats = np.column_stack([L[:, ng // 2], L[:, : ng // 4].mean(axis=1), L[:, -ng // 4 :].mean(axis=1)])
    return L, np.ones_like(L, dtype=bool), feats


# --------------------------------------------------------------------------- AC-S4-01
@pytest.mark.acceptance("AC-S4-01")
def test_geometric_mean_is_exp_mean_ln_of_original_window_spectra():
    run, _ = core_run("broad3", 2)
    sd = run.spectra
    rows = run.standard.rows
    m = geometric_mean_curve(sd, rows)
    direct = np.exp(np.nanmean(np.where(sd.valid[rows], np.log(sd.hv[rows]), np.nan), axis=0))
    np.testing.assert_allclose(m.hv[m.valid], direct[m.valid], rtol=1e-12)
    # unit replicate counts reproduce the observed mean exactly (no weights, no alignment)
    reps = replicate_means(sd.ln_hv[rows], sd.valid[rows], np.ones((1, rows.size), dtype=np.int64))
    np.testing.assert_allclose(reps[0][m.valid], m.ln_mean[m.valid], rtol=0, atol=1e-12)
    # a replicate with multiplicities equals the geometric mean of the explicitly repeated spectra
    counts = np.random.default_rng(5).integers(0, 3, size=rows.size)
    counts[0] = 1
    rep = replicate_means(sd.ln_hv[rows], sd.valid[rows], counts[None, :])[0]
    explicit = np.repeat(rows, counts)
    Lx, Vx = sd.ln_hv[explicit], sd.valid[explicit]
    ex = np.where(Vx, Lx, 0).sum(0) / Vx.sum(0)
    ok = Vx.sum(0) > 0
    np.testing.assert_allclose(rep[ok], ex[ok], rtol=0, atol=1e-12)
    # export reports the same mean
    _, su, _ = _std_uncertainty("broad3", 2)
    exp = export_curve(m, su.ci)
    vals = np.array([np.nan if v is None else v for v in exp["hv_geometric_mean"]])
    np.testing.assert_allclose(vals[m.valid], m.hv[m.valid], rtol=1e-12)


# --------------------------------------------------------------------------- AC-S4-02
@pytest.mark.acceptance("AC-S4-02")
def test_bootstrap_is_deterministic_for_recorded_seed():
    _, a, _ = _std_uncertainty("two_peaks", 1)
    _, b, _ = _std_uncertainty("two_peaks", 1)
    assert a.ci.info["seed"] == CFG.seed and a.ci.info["n_bootstrap"] == CFG.n_bootstrap
    np.testing.assert_array_equal(a.ci.lower_ln, b.ci.lower_ln)
    np.testing.assert_array_equal(a.ci.upper_ln, b.ci.upper_ln)
    np.testing.assert_array_equal(a.ci.replicates, b.ci.replicates)
    assert a.peak["bootstrap"] == b.peak["bootstrap"]
    run, _ = core_run("two_peaks", 1)
    cfg2 = run.config.model_copy(
        update={"uncertainty": run.config.uncertainty.model_copy(update={"seed": 7})}
    )
    sd = run.spectra
    band = search_band_mask(sd, cfg2)
    fam = run.discovery.plausible_families()[0]
    c = selection_uncertainty(
        sd, run.prep, run.standard.rows, run.discovery.mean, cfg2, band, fam, run.discovery.families
    )
    assert c.ci.info["seed"] == 7
    assert not np.array_equal(np.nan_to_num(c.ci.lower_ln), np.nan_to_num(a.ci.lower_ln))


# --------------------------------------------------------------------------- AC-S4-03
@pytest.mark.acceptance("AC-S4-03")
def test_resampling_draws_whole_spectral_vectors():
    rng = np.random.default_rng(3)
    n, ng = 40, 30
    scale = rng.standard_normal(n)
    shape = np.linspace(0.5, 2.0, ng)
    L = scale[:, None] * shape[None, :]  # every window is the same frequency profile times its own scalar
    V = np.ones_like(L, dtype=bool)
    plan = plan_original_time(np.arange(n) * 50.0, 0.0, 50.0, L[:, [0, -1]], CFG)
    reps = replicate_means(L, V, replicate_counts(plan, n, 500, rng))
    # joint resampling keeps each replicate proportional to the profile; per-bin resampling would not
    ratio = reps / shape[None, :]
    assert np.max(np.abs(ratio - ratio[:, :1])) < 1e-12
    # moving-block counts are one vector per replicate, summing to n
    pr = plan_regular(np.arange(n) * 50.0, L[:, [0, -1]], CFG, base=3)
    cnt = replicate_counts(pr, n, 200, rng)
    assert cnt.shape == (200, n) and np.all(cnt.sum(axis=1) == n)


# --------------------------------------------------------------------------- AC-S4-04
@pytest.mark.acceptance("AC-S4-04")
def test_correlated_regular_sequence_gets_blocks_and_wider_band_than_iid():
    rng = np.random.default_rng(11)
    n = 200
    L, V, F = _synthetic_L(0.3 * _ar1(n, 0.8, rng), seed=1)
    t = np.arange(n) * 50.0
    res = selected_mean_ci(
        L,
        V,
        np.ones(L.shape[1], bool),
        t,
        np.zeros(n, int),
        0.0,
        50.0,
        F,
        np.ones(L.shape[1], bool),
        CFG,
        n_boot=1000,
    )
    assert res.info["method"] == "circular_moving_block"
    assert res.info["block_length_windows"] > 1
    assert res.info["informative_blocks"] < n
    iid = plan_regular(t, F, CFG, base=1)
    lo, hi, _ = band_from_replicates(
        replicate_means(L, V, replicate_counts(iid, n, 1000, np.random.default_rng(CFG.seed))),
        np.ones(L.shape[1], bool),
    )
    w_block = np.median(res.upper_ln - res.lower_ln)
    w_iid = np.median(hi - lo)
    assert w_block > 1.5 * w_iid, (w_block, w_iid)


@pytest.mark.acceptance("AC-S4-04")
def test_clustered_bursts_do_not_acquire_iid_count():
    """45 windows in 3 bursts sharing a burst-level offset: effectively ~3 independent pieces."""
    rng = np.random.default_rng(4)
    starts = [0.0, 3000.0, 6000.0]
    t = np.concatenate([s + np.arange(15) * 50.0 for s in starts])
    offs = np.repeat(rng.standard_normal(3) * 0.4, 15) + 0.02 * rng.standard_normal(45)
    L, V, F = _synthetic_L(offs, seed=2)
    seg = np.repeat(np.arange(3), 15)
    res = selected_mean_ci(
        L, V, np.ones(L.shape[1], bool), t, seg, 0.0, 50.0, F, np.ones(L.shape[1], bool), CFG, n_boot=1000
    )
    assert res.info["method"] == "original_time_cluster"
    assert res.info["dependence"]["doublings"] >= 1
    # the guard fires instead of treating 45 dependent windows as 45 independent ones
    assert res.status == "ci_not_estimable"
    assert res.info["informative_blocks"] < 10
    assert np.all(np.isnan(res.lower_ln)) and np.all(np.isnan(res.upper_ln))


# --------------------------------------------------------------------------- AC-S4-05
@pytest.mark.acceptance("AC-S4-05")
def test_blocks_are_defined_in_original_time_and_gaps_are_preserved():
    t = np.concatenate([np.arange(12) * 50.0, 5000.0 + np.arange(12) * 50.0])
    seg = np.repeat([0, 1], 12)
    assert not is_regular(t, seg, CFG.regular_spacing_max_ratio)
    # a gap-compressed index would look perfectly regular
    assert is_regular(np.arange(24) * 50.0, np.zeros(24, int), CFG.regular_spacing_max_ratio)
    L, V, F = _synthetic_L(0.1 * np.random.default_rng(0).standard_normal(24))
    plan = plan_original_time(t, 0.0, 50.0, F, CFG)
    for c in np.unique(plan.clusters):
        members = t[plan.clusters == c]
        assert members.max() - members.min() < plan.block_length_s
        assert np.all(members < 5000) or np.all(members >= 5000)  # no block straddles the gap
    hist = plan.diagnostics["adjacent_block_correlation"][0]
    assert hist["adjacent_pairs"] == plan.n_blocks - 2  # the pair across the gap is not adjacent


@pytest.mark.acceptance("AC-S4-05")
def test_recording_with_gap_uses_original_time_blocks_end_to_end():
    rec, _ = generate_3c(duration_s=1800, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=21)
    seg = rec.segments[0]
    cut, gap = 900 * 50, 600 * 50
    rec.segments = [
        Segment(0, 0.0, seg.data[:, :cut].copy()),
        Segment(cut + gap, (cut + gap) / 50, seg.data[:, cut:].copy()),
    ]
    rec.diagnostics.gaps = [Gap(cut - 1, gap / 50)]
    run = run_core(rec, AnalysisConfig())
    sd = run.spectra
    fam = run.discovery.plausible_families()[0]
    su = selection_uncertainty(
        sd,
        run.prep,
        run.standard.rows,
        run.discovery.mean,
        run.config,
        search_band_mask(sd, run.config),
        fam,
        run.discovery.families,
    )
    assert su.ci.info["regular_sequence"] is False
    assert su.ci.info["method"] == "original_time_cluster"
    starts = sd.window.global_start[np.sort(run.standard.rows)] / 50.0
    assert np.any(starts < 900) and np.any(starts >= 1500)
    assert not np.any((starts > 900 - 50) & (starts < 1500))  # nothing invented inside the gap


# --------------------------------------------------------------------------- AC-S4-06
@pytest.mark.acceptance("AC-S4-06")
def test_too_few_blocks_returns_not_estimable():
    L, V, F = _synthetic_L(0.1 * np.random.default_rng(1).standard_normal(8))
    t = np.arange(8) * 50.0
    res = selected_mean_ci(
        L, V, np.ones(L.shape[1], bool), t, np.zeros(8, int), 0.0, 50.0, F, np.ones(L.shape[1], bool), CFG
    )
    assert res.status == "ci_not_estimable"
    assert "informative blocks" in res.status_reason
    assert np.all(np.isnan(res.lower_ln)) and np.all(np.isnan(res.upper_ln))
    one = selected_mean_ci(
        L[:1],
        V[:1],
        np.ones(L.shape[1], bool),
        t[:1],
        np.zeros(1, int),
        0.0,
        50.0,
        F[:1],
        np.ones(L.shape[1], bool),
        CFG,
    )
    assert one.status == "ci_not_estimable"


@pytest.mark.acceptance("AC-S4-06")
def test_zero_width_interval_is_never_reported():
    rng = np.random.default_rng(2)
    n = 30
    L, V, F = _synthetic_L(0.1 * rng.standard_normal(n), noise=0.05, seed=3)
    L[:, 5:10] = 0.7  # identical across windows: bootstrap spread is exactly zero there
    res = selected_mean_ci(
        L,
        V,
        np.ones(L.shape[1], bool),
        np.arange(n) * 50.0,
        np.zeros(n, int),
        0.0,
        50.0,
        F,
        np.ones(L.shape[1], bool),
        CFG,
        n_boot=500,
    )
    assert res.status == "ci_limited" and "zero bootstrap width" in res.status_reason
    assert np.all(np.isnan(res.lower_ln[5:10])) and np.all(np.isnan(res.upper_ln[5:10]))
    fin = np.isfinite(res.lower_ln) & np.isfinite(res.upper_ln)
    assert np.all(res.upper_ln[fin] - res.lower_ln[fin] > 1e-12)


@pytest.mark.acceptance("AC-S4-06")
@pytest.mark.parametrize("case", ["flat", "sdof2", "broad3", "two_peaks", "weak_low"])
def test_no_zero_width_on_pipeline_cases(case):
    _, su, _ = _std_uncertainty(case, 1)
    lo, hi = su.ci.lower_ln, su.ci.upper_ln
    fin = np.isfinite(lo) & np.isfinite(hi)
    assert np.all(hi[fin] - lo[fin] > 1e-12)


# --------------------------------------------------------------------------- AC-S4-07
@pytest.mark.acceptance("AC-S4-07")
def test_peak_bootstrap_tracks_no_peak_and_alternate_family_replicates():
    ng = 60
    ln_f = np.log(np.logspace(-1, 1, ng))
    x = np.arange(ng)

    def bump(c, a):
        return a * np.exp(-0.5 * ((x - c) / 2.5) ** 2)

    in_fam = [bump(20 + (k % 3) - 1, 1.0) + bump(45, 0.3) for k in range(60)]  # peak inside family [15, 25]
    no_peak = [0.01 * x for _ in range(25)]  # monotonic: no local maximum in the family
    alternate = [bump(20, 0.4) + bump(45, 1.2) for _ in range(15)]  # family peak exists, but global max in F2
    reps = np.vstack(in_fam + no_peak + alternate)
    out = peak_replicates(reps, np.ones(ng, bool), ln_f, ("F1", 15, 25), [("F2", 40, 50)], np.ones(ng, bool))
    assert out["n_replicates"] == 100
    assert out["fraction_no_peak_in_family"] == pytest.approx(0.25)
    assert out["fraction_peak_in_family"] == pytest.approx(0.75)
    assert out["fraction_global_max_in_other_family"] == pytest.approx(0.15)
    assert "conditional" in out["conditioning"] or "whose mean curve" in out["conditioning"]


@pytest.mark.acceptance("AC-S4-07")
def test_pipeline_peak_estimate_reports_outcome_fractions():
    _, su, _ = _std_uncertainty("two_peaks", 2)
    b = su.peak["bootstrap"]
    assert b is not None
    total = b["fraction_peak_in_family"] + b["fraction_no_peak_in_family"]
    assert total == pytest.approx(1.0)
    assert su.peak["status"] in {"peak_found", "no_peak_in_family_band"}


# --------------------------------------------------------------------------- AC-S4-08
@pytest.mark.acceptance("AC-S4-08")
def test_adding_rejected_data_outside_accepted_windows_does_not_change_ci():
    rec_a, _ = generate_3c(duration_s=1200, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=31)
    rec_b = copy.deepcopy(rec_a)
    rng = np.random.default_rng(0)
    extra = rng.standard_normal((3, 600 * 50)) * 1000.0
    extra[0] = 0.0  # dead north channel: every window here is rejected by basic QC
    start = rec_a.segments[0].stop_index + 100 * 50
    rec_b.segments.append(Segment(start, start / 50, extra))
    rec_b.diagnostics.gaps = [Gap(rec_a.segments[0].stop_index - 1, 100.0)]
    cfg = AnalysisConfig()
    ra, rb = run_core(rec_a, cfg), run_core(rec_b, cfg)
    assert rb.qc.counts().get("dead_channel", 0) > 0
    assert int((~rb.qc.passed).sum()) > int((~ra.qc.passed).sum())
    ga = ra.window.global_start[ra.standard.rows]
    gb = rb.window.global_start[rb.standard.rows]
    np.testing.assert_array_equal(ga, gb)
    np.testing.assert_array_equal(ra.spectra.ln_hv[ra.standard.rows], rb.spectra.ln_hv[rb.standard.rows])
    fam_a = ra.discovery.plausible_families()[0]
    fam_b = next(
        f
        for f in rb.discovery.plausible_families()
        if f.lo_index == fam_a.lo_index and f.hi_index == fam_a.hi_index
    )
    ua = selection_uncertainty(
        ra.spectra,
        ra.prep,
        ra.standard.rows,
        ra.discovery.mean,
        cfg,
        search_band_mask(ra.spectra, cfg),
        fam_a,
        ra.discovery.families,
    )
    ub = selection_uncertainty(
        rb.spectra,
        rb.prep,
        rb.standard.rows,
        rb.discovery.mean,
        cfg,
        search_band_mask(rb.spectra, cfg),
        fam_b,
        rb.discovery.families,
    )
    np.testing.assert_array_equal(ua.ci.lower_ln, ub.ci.lower_ln)
    np.testing.assert_array_equal(ua.ci.upper_ln, ub.ci.upper_ln)
    assert ua.ci.status == ub.ci.status
    assert "rejection" not in str(ua.ci.info).lower()


# --------------------------------------------------------------------------- AC-S4-09
@pytest.mark.acceptance("AC-S4-09")
def test_block_sensitivity_is_reported_and_base_band_is_kept():
    rng = np.random.default_rng(8)
    n = 120
    L, V, F = _synthetic_L(0.3 * _ar1(n, 0.6, rng), seed=4)
    t = np.arange(n) * 50.0
    ones = np.ones(L.shape[1], bool)
    res = selected_mean_ci(L, V, ones, t, np.zeros(n, int), 0.0, 50.0, F, ones, CFG, n_boot=1000)
    sens = res.info["sensitivity"]
    assert [s["factor"] for s in sens] == [1.0, *CFG.sensitivity_factors]
    widths = {s["factor"]: s["median_width_ln"] for s in sens if s["median_width_ln"] is not None}
    assert widths[0.5] < widths[1.0]  # a narrower alternative exists...
    assert np.median(res.upper_ln - res.lower_ln) == pytest.approx(widths[1.0])  # ...and is not chosen
    strict = CFG.model_copy(update={"sensitivity_width_ratio_limit": 1.0001})
    res2 = selected_mean_ci(L, V, ones, t, np.zeros(n, int), 0.0, 50.0, F, ones, strict, n_boot=1000)
    assert res2.status == "ci_limited" and "predeclared block lengths" in res2.status_reason
    np.testing.assert_array_equal(res2.lower_ln, res.lower_ln)


# --------------------------------------------------------------------------- AC-S4-10
@pytest.mark.acceptance("AC-S4-10")
def test_conditional_mean_ci_is_distinct_from_window_dispersion_and_one_band_exported():
    run, su, band = _std_uncertainty("sdof2", 1)
    mean = run.discovery.mean
    exp = export_curve(mean, su.ci)
    assert set(exp) == {"hv_geometric_mean", "ci95_lower", "ci95_upper", "count", "log_std"}
    assert su.ci.info["label"] == LABEL
    assert "not the uncertainty of the mean" in su.dispersion["label"]
    m = band & mean.valid & np.isfinite(su.ci.lower_ln)
    half = 0.5 * (su.ci.upper_ln - su.ci.lower_ln)[m]
    # the mean's CI is much narrower than the window-to-window spread it is NOT
    assert np.median(half) < 0.6 * np.median(mean.log_std[m])
    assert not np.allclose(half, mean.log_std[m])


@pytest.mark.acceptance("AC-S4-10")
def test_uncertainty_object_validates_against_analysis_schema():
    import jsonschema

    from hvsr_core.jsonutil import canonical_dumps, strict_loads
    from hvsr_core.schema_validation import load_schema

    schema = load_schema("analysis")
    doc = {"$defs": schema["$defs"], **schema["$defs"]["selection"]["properties"]["uncertainty"]}
    pe = {"$defs": schema["$defs"], **schema["$defs"]["peak_estimate"]}
    for case in ("sdof2", "weak_low"):
        _, su, _ = _std_uncertainty(case, 2)
        jsonschema.Draft202012Validator(doc).validate(strict_loads(canonical_dumps(su.ci.info)))
        jsonschema.Draft202012Validator(pe).validate(strict_loads(canonical_dumps(su.peak)))


# --------------------------------------------------------------------------- AC-S4-11
@pytest.mark.acceptance("AC-S4-11")
def test_multimodal_peak_replicates_are_ambiguous():
    ng = 80
    ln_f = np.log(np.logspace(-1, 1, ng))
    x = np.arange(ng)

    def bump(c):
        return np.exp(-0.5 * ((x - c) / 2.0) ** 2)

    reps = np.vstack([bump(30) if k % 2 else bump(42) for k in range(200)])
    out = peak_replicates(reps, np.ones(ng, bool), ln_f, ("F1", 25, 47), [], np.ones(ng, bool))
    assert out["status"] == "ambiguous_multimodal"
    assert len(out["modes_hz"]) == 2
    assert all(m >= 0.1 for m in out["mode_masses"])
    uni = peak_replicates(
        np.vstack([bump(36 + (k % 3) - 1) for k in range(200)]),
        np.ones(ng, bool),
        ln_f,
        ("F1", 25, 47),
        [],
        np.ones(ng, bool),
    )
    assert uni["status"] == "unimodal" and len(uni["modes_hz"]) == 1


# --------------------------------------------------------------------------- AC-S4-12
@pytest.mark.acceptance("AC-S4-12")
def test_bootstrap_uses_cached_spectra_without_fft(monkeypatch):
    import time

    import scipy.fft

    run, _ = core_run("two_peaks", 3)

    def boom(*a, **k):
        raise AssertionError("FFT recomputed during bootstrap")

    for mod, name in ((np.fft, "rfft"), (np.fft, "fft"), (scipy.fft, "rfft"), (scipy.fft, "fft")):
        monkeypatch.setattr(mod, name, boom)
    sd = run.spectra
    fam = run.discovery.plausible_families()[0]
    t0 = time.perf_counter()
    su = selection_uncertainty(
        sd,
        run.prep,
        run.standard.rows,
        run.discovery.mean,
        run.config,
        search_band_mask(sd, run.config),
        fam,
        run.discovery.families,
    )
    elapsed = time.perf_counter() - t0
    assert su.ci.info["n_bootstrap"] == 2000
    assert elapsed < 20.0
