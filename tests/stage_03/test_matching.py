"""Stage 3: R2_log matching, eligibility, greedy selection, and the CRITICAL no-alignment test.

Declared interpolation tolerance for noise-free windows built from a bank template (AC-S3-03):
1 - R2 <= 1e-2 for the raw 1 % damping template and 1 - R2 <= 5e-4 for every other template, raw or
Konno-Ohmachi conditioned. Derivation: a sweep of the true peak over 100 sub-grid positions of the
0.005-decade analysis grid gives worst cases 5.6e-3 (raw 1 %; its half-power width ~2 zeta = 0.02 in
ln f is under two grid steps), 1.4e-4 (raw 2 %), 1.5e-5 (raw 3 %), <= 2.1e-6 (raw >= 5 %), and
<= 1e-6 (conditioned b = 40). Tolerances are ~2x the swept worst case (docs/template-banks.md).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hvsr_core.config import AnalysisConfig
from hvsr_core.core import run_core
from hvsr_core.curves import geometric_mean_curve
from hvsr_core.matching import (
    batched_sse,
    estimate_peaks,
    greedy_select,
    match_family,
    sdof_for_families,
    select_family,
)
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_core.templates import (
    DEFAULT_SDOF_DAMPING,
    TemplateBank,
    get_bank,
    ko_continuous,
    resolve_sdof_bank,
    sdof_m,
)

IDX = np.arange(-160, 300)
F = 10.0 ** (IDX / 200)
LN_F = np.log(F)
CFG = AnalysisConfig()


def _band(f_lo, f_hi):
    return int(np.searchsorted(F, f_lo)), int(np.searchsorted(F, f_hi))


def _sdof_rows(bank, k, fps, aps, conditioned=False):
    z = float(bank.param_values[k, 0])
    rp = bank.anchor_ratio[k]
    if conditioned:
        h = lambda r: ko_continuous(lambda s: sdof_m(s, z), r, 40.0, 3.0)  # noqa: E731
    else:
        h = lambda r: sdof_m(r, z)  # noqa: E731
    peak = h(np.array([rp]))[0]
    return np.vstack([np.log(ap * h(F / fp * rp) / peak) for fp, ap in zip(fps, aps, strict=True)])


def _match(L, V=None, lo_hi=(0.9, 5.6), bank=None, cfg=CFG, df=0.02):
    V = np.ones_like(L, dtype=bool) if V is None else V
    lo, hi = _band(*lo_hi)
    return match_family(
        L, V, LN_F, df, np.arange(L.shape[0]), "F1", lo, hi, bank or get_bank("sdof-raw-v1"), cfg
    )


@pytest.mark.acceptance("AC-S3-03")
@pytest.mark.parametrize("bank_id", ["sdof-raw-v1", "sdof-ko40-v1"])
def test_known_templates_score_near_one_and_are_selected(bank_id):
    bank = get_bank(bank_id)
    rng = np.random.default_rng(11)
    cond = bank_id != "sdof-raw-v1"
    for k, z in enumerate(DEFAULT_SDOF_DAMPING):
        fps = np.exp(rng.uniform(0, math.log(5), 12))
        aps = rng.uniform(1.5, 8, 12)
        t = _match(_sdof_rows(bank, k, fps, aps, cond), bank=bank)
        assert all(s == "eligible" for s in t.status)
        assert np.all(t.best_k == k), (z, t.best_k)
        tol = 1e-2 if (z == 0.01 and not cond) else 5e-4
        assert np.all(1 - t.best_r2 <= tol), (z, t.best_r2.min())
        np.testing.assert_allclose(t.peaks.fp, fps, rtol=2e-3)


@pytest.mark.acceptance("AC-S3-04")
def test_detected_peak_is_damped_peak_and_equivalent_fn_is_reported_as_template_ratio():
    bank = get_bank("sdof-raw-v1")
    k = list(DEFAULT_SDOF_DAMPING).index(0.2)
    fn = 2.0
    row = np.log(sdof_m(F / fn, 0.2))[None, :]
    t = _match(row, bank=bank)
    fp = t.peaks.fp[0]
    assert fp == pytest.approx(fn * bank.anchor_ratio[k], rel=2e-3)  # 1.93 Hz, not 2.0
    assert abs(fp / fn - 1) > 0.03
    assert t.best_k[0] == k
    assert fp / bank.anchor_ratio[t.best_k[0]] == pytest.approx(fn, rel=2e-3)


@pytest.mark.acceptance("AC-S3-06")
def test_incomplete_fit_bands_are_rejected():
    bank = get_bank("sdof-raw-v1")
    L = _sdof_rows(bank, 4, [2.0, 2.0, 2.0], [3, 3, 3])
    V = np.ones_like(L, dtype=bool)
    V[0, np.searchsorted(F, 3.0)] = False  # gap inside its own fit band [1, 4] Hz
    V[1, np.searchsorted(F, 0.6)] = False  # gap inside the family union band [0.45, 11.2] but outside [1, 4]
    t = _match(L, V, lo_hi=(0.9, 5.6))
    assert t.status == ["incomplete_fit_band", "outside_common_band", "eligible"]
    assert np.all(np.isnan(t.r2[:2])) and np.all(t.best_k[:2] == -1)
    # fp/2 below the lowest grid frequency (0.158 Hz): never extrapolated
    low = _sdof_rows(bank, 4, [0.3], [3])
    t2 = _match(low, lo_hi=(0.25, 0.35))
    assert t2.status == ["incomplete_fit_band"] and t2.best_k[0] == -1
    assert F[0] > t2.fit_band_hz[0, 0]


@pytest.mark.acceptance("AC-S3-07")
def test_scores_ignore_data_outside_fit_band():
    bank = get_bank("sdof-ko40-v1")
    rng = np.random.default_rng(2)
    L = _sdof_rows(bank, 5, [2.0], [3.0], conditioned=True) + 0.05 * rng.standard_normal(F.size)
    L[0, np.searchsorted(F, 2.0) - 3 : np.searchsorted(F, 2.0) + 4] = (
        np.log(3.0) - 0.001 * (np.arange(7) - 3) ** 2
    )
    fam = (1.8, 2.2)  # peak-estimator domain lies inside the fit band
    base = _match(L.copy(), lo_hi=fam, bank=bank)
    assert base.status == ["eligible"]
    fp = base.peaks.fp[0]
    lnf = math.log(fp)
    a = int(np.searchsorted(LN_F, lnf - math.log(2) + 1e-12, side="right") - 1)
    b = int(np.searchsorted(LN_F, lnf + math.log(2) - 1e-12, side="left"))
    P = L.copy()
    P[0, :a] += 5.0 * rng.standard_normal(a)
    P[0, b + 1 :] += 5.0 * rng.standard_normal(F.size - b - 1)
    pert = _match(P, lo_hi=fam, bank=bank)
    np.testing.assert_array_equal(pert.r2, base.r2)
    np.testing.assert_array_equal(pert.peaks.fp, base.peaks.fp)
    Q = L.copy()
    Q[0, a + 5] += 0.3
    assert not np.array_equal(_match(Q, lo_hi=fam, bank=bank).r2, base.r2)


@pytest.mark.acceptance("AC-S3-08")
def test_negative_r2_retained_unclamped():
    row = 3.0 * (LN_F - math.log(2.0))
    i0 = np.searchsorted(F, 2.0)
    row[i0] = row[i0 - 1] + 0.5 * (row[i0 + 1] - row[i0 - 1]) + 0.2  # tiny local max at 2 Hz on a steep rise
    t = _match(row[None, :], lo_hi=(1.95, 2.05))
    assert t.status[0] == "no_meaningful_match"
    assert t.best_r2[0] < 0
    assert np.all(t.r2[0] < 0) and np.all(np.isfinite(t.r2[0]))


@pytest.mark.acceptance("AC-S3-09")
def test_flat_denominator_is_undefined_not_one():
    row = np.zeros(F.size)
    row[np.searchsorted(F, 2.0)] = 1e-5
    t = _match(row[None, :], lo_hi=(1.9, 2.1))
    assert t.status[0] == "undefined_r2"
    assert np.all(np.isnan(t.r2[0])) and np.isnan(t.best_r2[0]) and t.best_k[0] == -1


@pytest.mark.acceptance("AC-S3-10")
def test_manual_top_n_never_fills_with_invalid_matches():
    rec, _ = generate_3c(duration_s=1200, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=21)
    run = run_core(rec, CFG)
    fam = run.discovery.plausible_families()[0]
    cfg = AnalysisConfig.model_validate(
        {"sdof": {"selection_mode": "manual_top_n", "manual_n": 500, "r2_min": 0.9}}
    )
    bank = get_bank("sdof-ko40-v1")
    t = match_family(
        run.spectra.ln_hv,
        run.spectra.valid,
        np.log(run.grid.freq),
        run.spectra.df_hz,
        np.flatnonzero(run.qc.passed),
        fam.family_id,
        fam.lo_index,
        fam.hi_index,
        bank,
        cfg,
    )
    sel = select_family(t, run.window, run.spectra, cfg)
    assert sel.status == "insufficient_matches" and sel.requested_n == 500
    status = dict(zip(t.rows.tolist(), t.status, strict=True))
    assert all(status[r] == "eligible" for r in sel.selected)
    assert 0 < sel.selected.size < 500
    assert t.status_counts()["no_meaningful_match"] > 0  # poorer matches exist but were not used
    small = AnalysisConfig.model_validate({"sdof": {"selection_mode": "manual_top_n", "manual_n": 3}})
    s3 = select_family(t, run.window, run.spectra, small)
    assert s3.status == "ok" and s3.selected.size == 3
    np.testing.assert_array_equal(s3.selected, sel.selected[:3])


@pytest.mark.acceptance("AC-S3-11")
def test_greedy_removes_shared_samples_and_breaks_ties_deterministically():
    gs = np.array([0, 50, 100, 150, 250, 350])
    ids = ["a", "b", "c", "d", "e", "f"]
    score = np.array([0.9, 0.95, 0.9, 0.5, 0.7, 0.7])
    chosen = greedy_select(gs, ids, 100, score, np.ones(6, dtype=bool))
    # order b(.95) a(.9,0) c(.9,100) e(.7,250) f(.7,350) d(.5); a and c share samples with b [50,150);
    # e [250,350), f [350,450) and d [150,250) only touch neighbours end-to-start (half-open) -> kept
    assert chosen == [1, 4, 5, 3]
    # exact half-open adjacency and tie order
    gs2 = np.array([200, 100, 0, 100])
    ids2 = ["w3", "w2b", "w1", "w2a"]
    s2 = np.array([0.8, 0.8, 0.8, 0.8])
    assert greedy_select(gs2, ids2, 100, s2, np.ones(4, dtype=bool)) == [
        2,
        3,
        0,
    ]  # starts 0, 100 (id w2a < w2b), 200
    assert greedy_select(gs2, ids2, 101, s2, np.ones(4, dtype=bool)) == [2, 0]  # 0..101 overlaps 100
    assert greedy_select(gs2, ids2, 100, s2, np.array([False, True, True, False])) == [2, 1]


@pytest.mark.acceptance("AC-S3-16")
def test_peak_estimates_frozen_and_bank_independent():
    rng = np.random.default_rng(5)
    L = _sdof_rows(get_bank("sdof-raw-v1"), 3, [1.7, 2.9], [3, 4]) + 0.02 * rng.standard_normal((2, F.size))
    a = _match(L, bank=get_bank("sdof-raw-v1"))
    b = _match(L, bank=get_bank("sdof-ko40-v1"))
    np.testing.assert_array_equal(a.peaks.fp, b.peaks.fp)
    np.testing.assert_array_equal(a.peaks.ln_ap, b.peaks.ln_ap)
    lo, hi = _band(0.9, 5.6)
    pe = estimate_peaks(L, np.ones_like(L, dtype=bool), LN_F, np.arange(2), lo, hi, "parabolic_log")
    np.testing.assert_array_equal(pe.fp, a.peaks.fp)
    # y at q = 1 is the observed curve relative to the frozen Ap (no template-driven shift)
    assert np.all(np.abs(a.y[:, 64]) < 0.05)


@pytest.mark.acceptance("AC-S3-17")
def test_batched_sse_matches_direct():
    rng = np.random.default_rng(9)
    Y = rng.standard_normal((40, 129))
    T = rng.standard_normal((9, 129))
    w = np.full(129, 1 / 129)
    direct = np.array([[np.sum(w * (y - t) ** 2) for t in T] for y in Y])
    np.testing.assert_allclose(batched_sse(Y, T, w), direct, rtol=1e-12, atol=1e-14)
    bank = get_bank("sdof-raw-v1")
    L = _sdof_rows(bank, 6, [2.2], [3.0])
    t = _match(L, bank=bank)
    y = t.y[0]
    sst = np.sum((y - y.mean()) ** 2) / 129
    exact = 1 - np.sum((y - bank.log_t[6]) ** 2) / 129 / sst
    assert t.best_r2[0] == exact


@pytest.mark.acceptance("AC-S3-18")
def test_insufficient_fft_resolution_is_unrankable():
    bank = get_bank("sdof-raw-v1")
    L = _sdof_rows(bank, 6, [0.5, 2.0], [3.0, 3.0])
    t = _match(L, lo_hi=(0.4, 2.5), bank=bank, df=0.1)  # Lw = 10 s: [0.25, 1] Hz holds 8 bins
    assert t.status[0] == "insufficient_resolution"
    assert t.status[1] == "eligible"
    assert np.isnan(t.best_r2[0])


@pytest.mark.acceptance("AC-S3-19")
def test_multiple_families_ranked_and_selected_separately():
    rec, _ = generate_3c(
        duration_s=1200,
        fs=50,
        tf=TFSpec(
            "multi_peak",
            {
                "peaks": [
                    {"fc": 1.5, "amplitude": 4.0, "width_decades": 0.06},
                    {"fc": 6.0, "amplitude": 4.0, "width_decades": 0.06},
                ]
            },
        ),
        seed=22,
    )
    run = run_core(rec, CFG)
    bank, _ = resolve_sdof_bank("auto", 40.0)
    sels = sdof_for_families(run.spectra, run.qc.passed, run.discovery.families, bank, CFG)
    assert [s.family_id for s in sels] == [f.family_id for f in run.discovery.plausible_families()]
    assert len(sels) == 2
    lo, hi = sels
    assert np.nanmedian(lo.table.peaks.fp) == pytest.approx(1.5, rel=0.1)
    assert np.nanmedian(hi.table.peaks.fp) == pytest.approx(6.0, rel=0.1)
    assert lo.selected.size > 0 and hi.selected.size > 0
    assert not np.array_equal(lo.table.best_r2, hi.table.best_r2)


@pytest.mark.acceptance("AC-S3-20")
def test_ties_and_grid_boundary_flagged():
    raw = get_bank("sdof-raw-v1")
    t_last = _match(_sdof_rows(raw, 8, [2.0], [3.0]), bank=raw)
    t_first = _match(_sdof_rows(raw, 0, [2.0], [3.0]), bank=raw)
    t_mid = _match(_sdof_rows(raw, 4, [2.0], [3.0]), bank=raw)
    assert "grid_boundary_best" in t_last.flags[0] and "grid_boundary_best" in t_first.flags[0]
    assert "grid_boundary_best" not in t_mid.flags[0]
    dup = TemplateBank(
        manifest=dict(raw.manifest, bank_id="dup"),
        x=raw.x,
        log_t=np.vstack([raw.log_t[4], raw.log_t[4], raw.log_t[6]]),
        param_values=np.array([[0.05], [0.06], [0.1]]),
        anchor_ratio=raw.anchor_ratio[[4, 4, 6]],
    )
    t_tie = _match(_sdof_rows(raw, 4, [2.0], [3.0]), bank=dup)
    assert "tie" in t_tie.flags[0] and t_tie.best_k[0] == 0  # first in bank order


@pytest.mark.acceptance("AC-S3-12")
def test_switching_bank_changes_match_provenance():
    L = _sdof_rows(get_bank("sdof-raw-v1"), 4, [2.0], [3.0])
    a = _match(L, bank=get_bank("sdof-raw-v1"))
    b = _match(L, bank=get_bank("sdof-ko40-v1"))
    assert a.bank["bank_id"] == "sdof-raw-v1" and b.bank["bank_id"] == "sdof-ko40-v1"
    assert a.bank["data_sha256"] != b.bank["data_sha256"]
    assert a.bank["conditioning"]["kind"] == "raw" and b.bank["conditioning"]["ko_bandwidth"] == 40.0


@pytest.mark.acceptance("AC-S3-14")
def test_CRITICAL_final_mean_uses_original_unaligned_spectra():
    """Must FAIL if the final selected-window curve is peak-aligned or amplitude-normalized."""
    rec, _ = generate_3c(
        duration_s=2400,
        fs=50,
        tf=TFSpec("sdof", {"fn": 1.7, "zeta": 0.1}),
        seed=32,
        regimes=[
            {"start_s": s, "end_s": s + 300.0, "tf": TFSpec("sdof", {"fn": 2.0, "zeta": 0.1})}
            for s in (300.0, 900.0, 1500.0, 2100.0)
        ],
    )
    cfg = AnalysisConfig()
    run = run_core(rec, cfg)
    bank, _ = resolve_sdof_bank("auto", 40.0)
    sels = sdof_for_families(run.spectra, run.qc.passed, run.discovery.families, bank, cfg)
    assert len(sels) == 1
    sel = sels[0]
    rows = sel.selected
    assert rows.size >= 10
    sd = run.spectra
    # 1. final mean == unweighted geometric mean of ORIGINAL per-window curves on the physical Hz grid
    ref = geometric_mean_curve(sd, rows)
    np.testing.assert_array_equal(sel.mean.valid, ref.valid)
    np.testing.assert_allclose(sel.mean.hv[ref.valid], ref.hv[ref.valid], rtol=1e-13)
    direct = np.exp(np.mean(sd.ln_hv[rows], axis=0))
    ok = ref.valid & np.all(sd.valid[rows], axis=0)
    np.testing.assert_allclose(sel.mean.hv[ok], direct[ok], rtol=1e-12)
    # 2. per-window peak frequencies keep their original Hz scatter
    pos = np.searchsorted(sel.table.rows, rows)
    fps = sel.table.peaks.fp[pos]
    aps = np.exp(sel.table.peaks.ln_ap[pos])
    assert fps.max() / fps.min() > 1.2
    for r, f in zip(rows, fps, strict=True):
        i = int(np.argmin(np.abs(np.log(sd.grid.freq) - math.log(f))))
        assert abs(math.log(f) - math.log(sd.grid.freq[i])) <= 0.5 * (math.log(10) / 200) + 1e-12
        assert sd.hv[r, i] > 0
    # 3. an aligned mean would reach exp(mean ln Ap) at q = 1; the observed mean peak must be lower
    mean_peak = np.nanmax(np.where(ref.valid, sel.mean.hv, np.nan))
    aligned_peak = math.exp(np.mean(np.log(aps)))
    assert mean_peak < 0.97 * aligned_peak
    # 4. not amplitude-normalized
    assert mean_peak > 1.5
    # 5. aligned (q-domain) average differs from the reported curve
    q_mean = np.exp(np.nanmean(sel.table.y[pos], axis=0) + np.mean(np.log(aps)))
    assert abs(q_mean.max() - mean_peak) / mean_peak > 0.03
