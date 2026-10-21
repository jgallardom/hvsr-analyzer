"""SESAME D23.12 reliability (3) and peak-clarity (6) checks: a reporting-only diagnostic.

Evaluated from an already computed mean curve and its window dispersion; never feeds back into
selection, spectra, peaks, templates, or the bootstrap. Boundary conventions are documented in
docs/sesame-implementation.md (implementation conventions, not attributed to the source).
"""

from __future__ import annotations

import math

import numpy as np

from .versions import ALGORITHM_VERSIONS

SESAME_SOURCE = {
    "title": (
        "Guidelines for the implementation of the H/V spectral ratio technique on ambient vibrations: "
        "measurements, processing and interpretation"
    ),
    "issuer": "SESAME European research project, WP12, Deliverable D23.12",
    "date": "December 2004",
    "url": "http://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf",
    "sha256": None,
    "checksum_note": "not verified: the PDF is not fetched at analysis time and no checksum is bundled",
    "implementation": ALGORITHM_VERSIONS["sesame"],
    "conventions": "docs/sesame-implementation.md",
}

# (upper bound of band [exclusive], epsilon factor, theta for sigma_A, log10 theta for sigma_logH/V)
# Boundary values belong to the higher-frequency (stricter) band: convention C-1.
THRESHOLD_TABLE = (
    (0.2, 0.25, 3.0, 0.48),
    (0.5, 0.20, 2.5, 0.40),
    (1.0, 0.15, 2.0, 0.30),
    (2.0, 0.10, 1.78, 0.25),
    (math.inf, 0.05, 1.58, 0.20),
)
CHECK_IDS = ("R1", "R2", "R3", "C1", "C2", "C3", "C4", "C5", "C6")


def band_index(f0: float) -> int:
    for i, (upper, *_rest) in enumerate(THRESHOLD_TABLE):
        if f0 < upper:
            return i
    return len(THRESHOLD_TABLE) - 1


def epsilon_hz(f0: float) -> float:
    return THRESHOLD_TABLE[band_index(f0)][1] * f0


def theta(f0: float) -> dict:
    _, _, t_mult, t_log = THRESHOLD_TABLE[band_index(f0)]
    # C-2: the published sigma_A and log10 columns differ by rounding; use the stricter of the two.
    return {"sigma_a_column": t_mult, "log10_column_as_factor": 10**t_log, "applied": min(t_mult, 10**t_log)}


def sigma_a_reliability_threshold(f0: float) -> float:
    # C-3: the source gives 2 for f0 > 0.5 Hz and 3 for f0 < 0.5 Hz; f0 = 0.5 Hz uses the stricter 2.
    return 3.0 if f0 < 0.5 else 2.0


def compare(measured: float, comparison: str, threshold: float) -> str:
    """Single strict comparator for every scalar check (convention C-4: equality fails)."""
    ok = measured > threshold if comparison == ">" else measured < threshold
    return "pass" if ok else "fail"


def _check(
    cid: str, name: str, status: str, measured=None, threshold=None, comparison=None, reason=None, **extra
) -> dict:
    d = {
        "id": cid,
        "criterion": name,
        "status": status,
        "measured": measured,
        "threshold": threshold,
        "comparison": comparison,
        "reason": reason,
    }
    d.update(extra)
    return d


def _ne(cid: str, name: str, reason: str) -> dict:
    return _check(cid, name, "not_evaluable", reason=reason)


def _interp_ln(freq: np.ndarray, y: np.ndarray, valid: np.ndarray, f: float) -> float | None:
    """Linear interpolation in ln f between the two bracketing grid points; both must be valid."""
    j = int(np.searchsorted(freq, f))
    if (
        j <= 0
        or j >= freq.size
        or not (valid[j - 1] and valid[j])
        or not (np.isfinite(y[j - 1]) and np.isfinite(y[j]))
    ):
        return None
    lf = np.log(freq)
    w = (math.log(f) - lf[j - 1]) / (lf[j] - lf[j - 1])
    return float((1 - w) * y[j - 1] + w * y[j])


def _interval(freq: np.ndarray, lo: float, hi: float, closed: bool) -> np.ndarray:
    return (freq >= lo) & (freq <= hi) if closed else (freq > lo) & (freq < hi)


def _covered(freq: np.ndarray, lo: float, hi: float) -> bool:
    return bool(freq.size and freq[0] <= lo and freq[-1] >= hi)


def _parabolic(lf: np.ndarray, y: np.ndarray, i: int) -> float:
    if 0 < i < y.size - 1 and np.all(np.isfinite(y[i - 1 : i + 2])):
        d = y[i - 1] - 2 * y[i] + y[i + 1]
        if d < 0:
            off = float(np.clip(0.5 * (y[i - 1] - y[i + 1]) / d, -0.5, 0.5))
            return float(math.exp(lf[i] + off * (lf[1] - lf[0])))
    return float(math.exp(lf[i]))


def evaluate_peak(
    *,
    f0: float,
    a0: float,
    freq: np.ndarray,
    hv: np.ndarray,
    log_std: np.ndarray,
    valid: np.ndarray,
    family_lo_hz: float,
    family_hi_hz: float,
    window_peaks_hz: list[float | None],
    lw_s: float,
    nw: int,
) -> dict:
    """All nine checks for one peak of one selection's mean curve (A = geometric mean, sigma_A = exp(std ln))."""
    freq = np.asarray(freq, float)
    hv = np.asarray(hv, float)
    log_std = np.asarray(log_std, float)
    ok = np.asarray(valid, bool) & np.isfinite(hv)
    ok_s = ok & np.isfinite(log_std)
    sig_a = np.exp(log_std)
    checks = []

    # R1  f0 > 10 / Lw
    thr = 10.0 / lw_s
    checks.append(_check("R1", "f0 > 10 / Lw", compare(f0, ">", thr), f0, thr, ">"))
    # R2  nc = Lw * nw * f0 > 200
    nc = lw_s * nw * f0
    checks.append(
        _check("R2", "nc = Lw * nw * f0 > 200", compare(nc, ">", 200.0), nc, 200.0, ">", lw_s=lw_s, nw=nw)
    )
    # R3  sigma_A(f) < 2 (f0 > 0.5 Hz) or < 3 (f0 < 0.5 Hz) for 0.5 f0 < f < 2 f0  (universal)
    name = "sigma_A(f) < theta for 0.5 f0 < f < 2 f0"
    t3 = sigma_a_reliability_threshold(f0)
    m = _interval(freq, 0.5 * f0, 2.0 * f0, closed=False)
    vals = sig_a[m & ok_s]
    if vals.size and compare(float(vals.max()), "<", t3) == "fail":
        checks.append(_check("R3", name, "fail", float(vals.max()), t3, "<", n_bins=int(vals.size)))
    elif _covered(freq, 0.5 * f0, 2.0 * f0) and np.all(ok_s[m]) and m.any():
        checks.append(_check("R3", name, "pass", float(vals.max()), t3, "<", n_bins=int(vals.size)))
    else:
        checks.append(
            _ne(
                "R3",
                name,
                "dispersion not available on the whole interval (0.5 f0, 2 f0); no violating bin observed",
            )
        )

    # C1 / C2  existential half-amplitude witnesses on [f0/4, f0] and [f0, 4 f0]
    for cid, lo, hi, label in (
        ("C1", f0 / 4, f0, "f- in [f0/4, f0]"),
        ("C2", f0, 4 * f0, "f+ in [f0, 4 f0]"),
    ):
        name = f"exists {label} with A(f) < A0 / 2"
        m = _interval(freq, lo, hi, closed=True)
        sel = m & ok
        wit = np.flatnonzero(sel & (hv < a0 / 2))
        if wit.size:
            k = wit[np.argmin(np.abs(np.log(freq[wit] / f0)))]
            checks.append(
                _check(cid, name, "pass", float(hv[k]), a0 / 2, "<", witness_frequency_hz=float(freq[k]))
            )
        elif _covered(freq, lo, hi) and np.all(ok[m]) and m.any():
            checks.append(_check(cid, name, "fail", float(hv[sel].min()), a0 / 2, "<"))
        else:
            checks.append(
                _ne(cid, name, "no witness on the observed part of a truncated or partly invalid interval")
            )
    # C3  A0 > 2
    checks.append(_check("C3", "A0 > 2", compare(a0, ">", 2.0), a0, 2.0, ">"))
    # C4  peaks of A * sigma_A and A / sigma_A within +/- 5 % of f0 (strict: convention C-4)
    name = "f_peak[A(f) x/÷ sigma_A(f)] within +/-5 % of f0"
    fb = (freq >= family_lo_hz) & (freq <= family_hi_hz) & ok_s
    if fb.sum() >= 3:
        lf = np.log(freq)
        idx = np.flatnonzero(fb)
        res = {}
        for key, curve in (("upper", np.log(hv) + log_std), ("lower", np.log(hv) - log_std)):
            c = np.where(fb, curve, -np.inf)
            i = int(idx[np.argmax(c[idx])])
            res[key] = _parabolic(lf, np.where(ok_s, curve, np.nan), i)
        dev = max(abs(res["upper"] - f0), abs(res["lower"] - f0)) / f0
        checks.append(
            _check(
                "C4",
                name,
                compare(dev, "<", 0.05),
                dev,
                0.05,
                "<",
                peak_upper_hz=res["upper"],
                peak_lower_hz=res["lower"],
            )
        )
    else:
        checks.append(_ne("C4", name, "fewer than 3 valid bins with dispersion inside the family band"))
    # C5  sigma_f < epsilon(f0)
    name = "sigma_f < epsilon(f0)"
    fps = np.array([p for p in window_peaks_hz if p is not None and np.isfinite(p)], float)
    eps = epsilon_hz(f0)
    if fps.size >= 2 and fps.size >= 0.5 * max(nw, 1):
        sf = float(np.std(fps, ddof=1))
        checks.append(
            _check(
                "C5",
                name,
                compare(sf, "<", eps),
                sf,
                eps,
                "<",
                n_windows_with_peak=int(fps.size),
                n_windows=nw,
            )
        )
    else:
        checks.append(
            _ne(
                "C5",
                name,
                f"only {fps.size} of {nw} windows have a peak in the family band (convention C-5 requires >= 2 and >= 50 %)",
            )
        )
    # C6  sigma_A(f0) < theta(f0)
    name = "sigma_A(f0) < theta(f0)"
    ls0 = _interp_ln(freq, log_std, ok_s, f0)
    th = theta(f0)
    if ls0 is None:
        checks.append(_ne("C6", name, "dispersion not available at f0"))
    else:
        s0 = math.exp(ls0)
        checks.append(
            _check("C6", name, compare(s0, "<", th["applied"]), s0, th["applied"], "<", theta_columns=th)
        )

    rel = [c for c in checks if c["id"].startswith("R")]
    clr = [c for c in checks if c["id"].startswith("C")]
    n_pass = sum(c["status"] == "pass" for c in clr)
    n_ne = sum(c["status"] == "not_evaluable" for c in clr)
    if all(c["status"] == "pass" for c in rel):
        rel_status = "reliable"
    elif any(c["status"] == "fail" for c in rel):
        rel_status = "not_reliable"
    else:
        rel_status = "not_evaluable"
    if n_pass >= 5:
        clr_status = "clear"
    elif n_pass + n_ne < 5:
        clr_status = "not_clear"
    else:
        clr_status = "not_evaluable"
    return {
        "f0_hz": f0,
        "a0": a0,
        "sigma_a_definition": "exp(sample std of ln A over selected windows); multiplicative factor",
        "reliability": {"status": rel_status, "checks": rel},
        "clarity": {
            "status": clr_status,
            "passed": n_pass,
            "not_evaluable": n_ne,
            "required": 5,
            "checks": clr,
        },
    }


def not_requested() -> dict:
    return {"status": "not_requested"}


def no_defensible_peak(reason: str) -> dict:
    return {
        "status": "not_evaluable",
        "reason": reason,
        "not_evaluable_checks": list(CHECK_IDS),
        "note": "No numerical maximum is substituted for a fundamental-frequency claim.",
        "source": SESAME_SOURCE,
    }
