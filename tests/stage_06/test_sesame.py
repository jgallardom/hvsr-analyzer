"""Stage 6: SESAME D23.12 reliability/clarity checks, boundary conventions, not-evaluable handling."""

from __future__ import annotations

import math

import numpy as np
import pytest

from hvsr_core import sesame
from hvsr_core.sesame import THRESHOLD_TABLE, band_index, epsilon_hz, evaluate_peak, theta

FREQ = 10.0 ** (np.arange(-260, 341) / 200.0)  # 0.05 - 50 Hz, 200 points per decade
I0 = 260 + 80  # grid point 10**0.4 = 2.512 Hz (band > 2 Hz)
F0 = float(FREQ[I0])


def _curves(f0=F0, a0=4.0, width=0.1, floor=1.0):
    x = np.log(FREQ / f0)
    return floor + (a0 - floor) * np.exp(-(x**2) / (2 * width**2))


def _kw(**over):
    f0 = over.pop("f0", F0)
    kw = {
        "f0": f0,
        "a0": 4.0,
        "freq": FREQ,
        "hv": _curves(f0),
        "log_std": np.full(FREQ.size, 0.2),
        "valid": np.ones(FREQ.size, bool),
        "family_lo_hz": F0 / 1.5,
        "family_hi_hz": F0 * 1.5,
        "window_peaks_hz": [F0 * (1 + 0.002 * k) for k in range(-5, 5)],
        "lw_s": 50.0,
        "nw": 10,
    }
    kw.update(over)
    return kw


def _check(res: dict, cid: str) -> dict:
    return next(c for c in res["reliability"]["checks"] + res["clarity"]["checks"] if c["id"] == cid)


def _solve(measure, p0: float, target: float, increasing: bool = True, max_steps: int = 100000):
    """Parameter p (stepped by one ulp) with measure(p) == target exactly.

    Returns (p, None) when found, or (None, (p_below, p_above)) for adjacent parameters whose
    measured values straddle a target that no float64 input reproduces.
    """
    p, prev_dir = p0, None
    for _ in range(max_steps):
        m = measure(p)
        if m == target:
            return p, None
        up = (m < target) == increasing
        if prev_dir is not None and up != prev_dir:
            q = np.nextafter(p, math.inf if up else -math.inf)
            lo, hi = (p, q) if up else (q, p)
            return None, (lo, hi) if increasing else (hi, lo)
        prev_dir = up
        p = np.nextafter(p, math.inf if up else -math.inf)
    raise AssertionError("solver did not converge")


def _three(cid: str, build, p_at: float, rel=1e-9):
    """Statuses just below, at, and above the threshold in the measured quantity."""
    at = _check(evaluate_peak(**build(p_at)), cid)
    assert at["measured"] == at["threshold"], (at["measured"], at["threshold"])
    lo = _check(evaluate_peak(**build(p_at * (1 - rel))), cid)
    hi = _check(evaluate_peak(**build(p_at * (1 + rel))), cid)
    return lo, at, hi


@pytest.mark.acceptance("AC-S6-01")
def test_clean_peak_reliable_and_clear_with_measured_values():
    res = evaluate_peak(**_kw())
    assert res["reliability"]["status"] == "reliable"
    assert res["clarity"]["status"] == "clear" and res["clarity"]["passed"] == 6
    for c in res["reliability"]["checks"] + res["clarity"]["checks"]:
        assert c["status"] == "pass"
        assert c["measured"] is not None and c["threshold"] is not None and c["comparison"] in ("<", ">")
    assert [c["id"] for c in res["reliability"]["checks"]] == ["R1", "R2", "R3"]
    assert [c["id"] for c in res["clarity"]["checks"]] == ["C1", "C2", "C3", "C4", "C5", "C6"]
    assert "exp(sample std of ln" in res["sigma_a_definition"]


@pytest.mark.acceptance("AC-S6-01")
def test_at_least_five_of_six_rule_and_reliability_states():
    spread = [F0 * (1 + 0.2 * k) for k in range(-5, 5)]  # sigma_f >> 0.05 f0 -> C5 fails
    five = evaluate_peak(**_kw(window_peaks_hz=spread))
    assert _check(five, "C5")["status"] == "fail"
    assert five["clarity"]["passed"] == 5 and five["clarity"]["status"] == "clear"

    wide = np.full(FREQ.size, math.log(1.8))  # sigma_A(f0) = 1.8 > 1.58 -> C6 fails; R3 still < 2
    four = evaluate_peak(**_kw(window_peaks_hz=spread, log_std=wide))
    assert _check(four, "C6")["status"] == "fail"
    assert four["clarity"]["passed"] == 4 and four["clarity"]["status"] == "not_clear"

    four_ne = evaluate_peak(**_kw(window_peaks_hz=[None] * 10, log_std=wide))
    assert _check(four_ne, "C5")["status"] == "not_evaluable"
    assert (four_ne["clarity"]["passed"], four_ne["clarity"]["not_evaluable"]) == (4, 1)
    assert four_ne["clarity"]["status"] == "not_evaluable"  # 5 still possible

    three = evaluate_peak(**_kw(window_peaks_hz=[None] * 10, log_std=wide, a0=2.0, hv=_curves(a0=2.0)))
    assert three["clarity"]["passed"] <= 3 and three["clarity"]["status"] == "not_clear"

    assert evaluate_peak(**_kw(lw_s=1.0))["reliability"]["status"] == "not_reliable"  # R1: f0 < 10/Lw
    trunc = FREQ > F0 * 0.6  # (0.5 f0, 2 f0) not fully covered, no violating bin
    r = evaluate_peak(
        **_kw(
            freq=FREQ[trunc],
            hv=_curves()[trunc],
            log_std=np.full(trunc.sum(), 0.2),
            valid=np.ones(trunc.sum(), bool),
        )
    )
    assert _check(r, "R3")["status"] == "not_evaluable"
    assert r["reliability"]["status"] == "not_evaluable"


@pytest.mark.acceptance("AC-S6-02")
@pytest.mark.parametrize("t", [0.05, 0.2, 2.0, 200.0, 1.58, 10**0.25])
def test_strict_comparator_at_boundary(t):
    below, above = float(np.nextafter(t, 0)), float(np.nextafter(t, math.inf))
    assert [sesame.compare(x, ">", t) for x in (below, t, above)] == ["fail", "fail", "pass"]
    assert [sesame.compare(x, "<", t) for x in (below, t, above)] == ["pass", "fail", "fail"]


@pytest.mark.acceptance("AC-S6-02")
def test_band_boundaries_convention_c1():
    for k, (upper, eps, t_mult, t_log) in enumerate(THRESHOLD_TABLE[:-1]):
        below = float(np.nextafter(upper, 0))
        assert band_index(below) == k
        assert band_index(upper) == k + 1  # boundary belongs to the higher (stricter) band
        assert epsilon_hz(upper) == THRESHOLD_TABLE[k + 1][1] * upper
        assert epsilon_hz(below) == eps * below
        assert theta(below)["applied"] == min(t_mult, 10**t_log)
        assert theta(upper)["applied"] == min(THRESHOLD_TABLE[k + 1][2], 10 ** THRESHOLD_TABLE[k + 1][3])
    # C-2: the smaller of the two published columns is applied
    assert theta(1.5) == {"sigma_a_column": 1.78, "log10_column_as_factor": 10**0.25, "applied": 10**0.25}
    assert theta(0.7)["applied"] == 10**0.30 and theta(3.0)["applied"] == 1.58


@pytest.mark.acceptance("AC-S6-02")
def test_r1_boundary():
    lo, at, hi = _three("R1", lambda f0: _kw(f0=f0), 0.2)
    assert (lo["status"], at["status"], hi["status"]) == ("fail", "fail", "pass")


@pytest.mark.acceptance("AC-S6-02")
def test_r2_boundary():
    lo, at, hi = _three("R2", lambda f0: _kw(f0=f0, lw_s=50.0, nw=4), 1.0)
    assert at["measured"] == 200.0
    assert (lo["status"], at["status"], hi["status"]) == ("fail", "fail", "pass")


@pytest.mark.acceptance("AC-S6-02")
@pytest.mark.parametrize(
    ("f0", "threshold"),
    [(F0, 2.0), (0.5, 2.0), (float(np.nextafter(0.5, 0)), 3.0), (0.3, 3.0)],
    ids=["f0>0.5", "f0=0.5(C-3)", "f0<0.5", "f0=0.3"],
)
def test_r3_boundary(f0, threshold):
    assert sesame.sigma_a_reliability_threshold(f0) == threshold

    def build(c):
        return _kw(f0=f0, hv=_curves(f0), log_std=np.full(FREQ.size, c))

    def measured(c):
        return _check(evaluate_peak(**build(c)), "R3")["measured"]

    c_at, straddle = _solve(measured, math.log(threshold), threshold)
    if c_at is not None:
        lo, at, hi = _three("R3", build, c_at)
        assert (lo["status"], at["status"], hi["status"]) == ("pass", "fail", "fail")
    else:
        # exp() has no float64 preimage of this threshold: test the adjacent reachable values;
        # equality itself is covered by test_strict_comparator_at_boundary.
        c_lo, c_hi = straddle
        assert np.nextafter(c_lo, math.inf) == c_hi
        assert measured(c_lo) < threshold < measured(c_hi)
        assert _check(evaluate_peak(**build(c_lo)), "R3")["status"] == "pass"
        assert _check(evaluate_peak(**build(c_hi)), "R3")["status"] == "fail"


@pytest.mark.acceptance("AC-S6-02")
def test_r3_universal_violation_and_open_interval():
    ls = np.full(FREQ.size, 0.2)
    ls[np.argmin(np.abs(FREQ - 1.5 * F0))] = math.log(5.0)
    assert _check(evaluate_peak(**_kw(log_std=ls)), "R3")["status"] == "fail"
    trunc = FREQ > F0 * 0.6  # truncated but a valid violating bin establishes the fail
    r = evaluate_peak(
        **_kw(freq=FREQ[trunc], hv=_curves()[trunc], log_std=ls[trunc], valid=np.ones(trunc.sum(), bool))
    )
    assert _check(r, "R3")["status"] == "fail"
    # open interval: a violation exactly at 2 f0 is outside (0.5 f0, 2 f0)
    g = F0 * 2.0 ** (np.arange(-60, 61) / 20.0)  # exact 0.5 f0 and 2 f0 grid points
    ls_g = np.full(g.size, 0.2)
    ls_g[-41] = math.log(5.0)
    assert g[-41] == 2 * F0
    r = evaluate_peak(
        **_kw(
            freq=g,
            hv=np.interp(np.log(g), np.log(FREQ), _curves()),
            log_std=ls_g,
            valid=np.ones(g.size, bool),
        )
    )
    assert _check(r, "R3")["status"] == "pass"


@pytest.mark.acceptance("AC-S6-02")
@pytest.mark.parametrize("cid", ["C1", "C2"])
def test_half_amplitude_boundary(cid):
    def build(m):
        hv = np.full(FREQ.size, m)
        hv[I0] = 4.0
        return _kw(hv=hv)

    lo, at, hi = _three(cid, build, 2.0)
    assert (lo["status"], at["status"], hi["status"]) == ("pass", "fail", "fail")


@pytest.mark.acceptance("AC-S6-02")
def test_half_amplitude_closed_interval_and_truncation():
    g = F0 * 2.0 ** (np.arange(-40, 41) / 20.0)  # exactly [f0/4, 4 f0]
    assert g[0] == F0 / 4 and g[-1] == 4 * F0
    hv = np.full(g.size, 3.0)
    hv[40] = 4.0
    hv[0] = hv[-1] = 1.9  # witnesses only at the closed edges
    r = evaluate_peak(**_kw(freq=g, hv=hv, log_std=np.full(g.size, 0.2), valid=np.ones(g.size, bool)))
    assert _check(r, "C1")["status"] == "pass" and _check(r, "C1")["witness_frequency_hz"] == F0 / 4
    assert _check(r, "C2")["status"] == "pass" and _check(r, "C2")["witness_frequency_hz"] == 4 * F0
    # truncated interval: no witness -> not evaluable (cannot fail); witness -> pass
    t = g[10:]
    hv_t = np.full(t.size, 3.0)
    hv_t[30] = 4.0
    r = evaluate_peak(**_kw(freq=t, hv=hv_t, log_std=np.full(t.size, 0.2), valid=np.ones(t.size, bool)))
    assert _check(r, "C1")["status"] == "not_evaluable"
    hv_t[2] = 1.0
    r = evaluate_peak(**_kw(freq=t, hv=hv_t, log_std=np.full(t.size, 0.2), valid=np.ones(t.size, bool)))
    assert _check(r, "C1")["status"] == "pass"
    # invalid bins inside a fully covered interval behave like truncation
    valid = np.ones(g.size, bool)
    valid[5] = False
    hv2 = np.full(g.size, 3.0)
    hv2[40] = 4.0
    r = evaluate_peak(**_kw(freq=g, hv=hv2, log_std=np.full(g.size, 0.2), valid=valid))
    assert _check(r, "C1")["status"] == "not_evaluable" and _check(r, "C2")["status"] == "fail"


@pytest.mark.acceptance("AC-S6-02")
def test_c3_boundary():
    lo, at, hi = _three("C3", lambda a0: _kw(a0=a0), 2.0)
    assert (lo["status"], at["status"], hi["status"]) == ("fail", "fail", "pass")


@pytest.mark.acceptance("AC-S6-02")
def test_c4_boundary_strict_five_percent():
    # Curves peak exactly at 10.5 Hz (symmetric neighbours: zero parabolic offset); f0 = 10 Hz gives
    # a deviation of exactly (10.5 - 10) / 10 == 0.05 in float64.
    g = 10.5 * 2.0 ** (np.arange(-120, 121) / 40.0)
    hv = np.full(g.size, 1.5)
    hv[120] = 4.0

    def c4(f0):
        kw = _kw(f0=f0, freq=g, hv=hv, log_std=np.full(g.size, 0.2), valid=np.ones(g.size, bool))
        return _check(evaluate_peak(**{**kw, "family_lo_hz": 9.0, "family_hi_hz": 12.0}), "C4")

    at = c4(10.0)
    assert at["peak_upper_hz"] == at["peak_lower_hz"] == 10.5
    assert at["measured"] == 0.05 and at["status"] == "fail"
    assert c4(10.0 * (1 + 1e-9))["status"] == "pass"  # deviation just below 5 %
    assert c4(10.0 * (1 - 1e-9))["status"] == "fail"
    assert c4(10.5 / 0.95 * (1 - 1e-9))["status"] == "pass"  # f0 above the peak: 5 % on the other side
    assert c4(10.5 / 0.95 * (1 + 1e-9))["status"] == "fail"
    narrow = _kw(family_lo_hz=F0 * 0.999, family_hi_hz=F0 * 1.001)
    assert _check(evaluate_peak(**narrow), "C4")["status"] == "not_evaluable"


@pytest.mark.acceptance("AC-S6-02")
def test_c5_boundary_and_window_requirements():
    peaks = [2.0 * F0 / 2.512, 2.2 * F0 / 2.512, None, None]
    sf = float(np.std([p for p in peaks if p is not None], ddof=1))

    def build(f0):
        return _kw(f0=f0, window_peaks_hz=peaks, nw=4)

    f_at, _ = _solve(lambda f0: epsilon_hz(f0), sf / 0.05, sf)
    assert f_at is not None and band_index(f_at) == 4
    at = _check(evaluate_peak(**build(f_at)), "C5")
    assert at["measured"] == at["threshold"] == sf and at["status"] == "fail"
    assert (
        _check(evaluate_peak(**build(f_at * (1 + 1e-9))), "C5")["status"] == "pass"
    )  # epsilon above sigma_f
    assert _check(evaluate_peak(**build(f_at * (1 - 1e-9))), "C5")["status"] == "fail"
    # C-5: >= 2 windows with an in-family peak and >= 50 % of nw
    assert _check(evaluate_peak(**_kw(window_peaks_hz=peaks, nw=5)), "C5")["status"] == "not_evaluable"
    assert _check(evaluate_peak(**_kw(window_peaks_hz=[F0, None], nw=2)), "C5")["status"] == "not_evaluable"
    assert _check(evaluate_peak(**_kw(window_peaks_hz=peaks, nw=4)), "C5")["status"] in ("pass", "fail")


@pytest.mark.acceptance("AC-S6-02")
@pytest.mark.parametrize("f0_index", [I0, 260 + 20, 260 - 80], ids=["band>2Hz", "band1-2Hz", "band0.2-0.5Hz"])
def test_c6_boundary(f0_index):
    f0 = float(FREQ[f0_index])
    th = theta(f0)["applied"]

    def build(c):
        return _kw(f0=f0, hv=_curves(f0), log_std=np.full(FREQ.size, c))

    def measured(c):
        return _check(evaluate_peak(**build(c)), "C6")["measured"]

    c_at, straddle = _solve(measured, math.log(th), th)
    if c_at is not None:
        lo, at, hi = _three("C6", build, c_at)
        assert (lo["status"], at["status"], hi["status"]) == ("pass", "fail", "fail")
    else:
        c_lo, c_hi = straddle
        assert np.nextafter(c_lo, math.inf) == c_hi
        assert measured(c_lo) < th < measured(c_hi)
        assert _check(evaluate_peak(**build(c_lo)), "C6")["status"] == "pass"
        assert _check(evaluate_peak(**build(c_hi)), "C6")["status"] == "fail"


@pytest.mark.acceptance("AC-S6-11")
def test_not_requested_and_no_defensible_peak():
    assert sesame.not_requested() == {"status": "not_requested"}
    r = sesame.no_defensible_peak("flat curve")
    assert r["status"] == "not_evaluable"
    assert r["not_evaluable_checks"] == ["R1", "R2", "R3", "C1", "C2", "C3", "C4", "C5", "C6"]
    assert "No numerical maximum" in r["note"]
    assert r["source"]["url"].endswith("HV_User_Guidelines.pdf")
    assert r["source"]["sha256"] is None and "not verified" in r["source"]["checksum_note"]
