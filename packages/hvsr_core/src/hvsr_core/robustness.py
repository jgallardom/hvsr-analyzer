"""Run A (primary Lw) vs run B (1.5 x Lw) comparison record (robust-lw1.5-v1, decision D-060).

Tolerances are predeclared research criteria tested on known signals, not SESAME rules. The two
runs use the same waveform: they are correlated and never averaged or counted as independent.
"""

from __future__ import annotations

import math

import numpy as np

from .jsonutil import canonical_hash
from .versions import ALGORITHM_VERSIONS, SCHEMA_VERSIONS

CRITERIA = {
    "version": ALGORITHM_VERSIONS["robustness"],
    "family_match_max_abs_log10_ratio": 0.1,
    "fp_max_abs_ln_ratio": 0.05,
    "ap_max_abs_ln_ratio": 0.20,
    "curve_max_median_abs_ln_diff": 0.10,
    "resolved_min_cycles_per_window": 10.0,
    "resolved_max_fft_resolution_over_fp": 0.10,
}
NOTES = [
    "Runs A and B use the same waveform: they are correlated and are not independent repeated measurements.",
    "Agreement tolerances are predeclared research criteria (D-060), not SESAME rules.",
    "Confidence-interval overlap alone is not an equivalence test and is not used as one.",
    "Neither run is preferred; disagreement is reported as sensitivity.",
]


def _nan(v):
    return np.array([np.nan if x is None else x for x in v], dtype=float)


def _width_ln(freq: np.ndarray, hv: np.ndarray, fp: float, ap: float) -> float | None:
    i = int(np.argmin(np.abs(np.log(freq / fp))))
    thr = ap / math.sqrt(2.0)
    if not np.isfinite(hv[i]) or hv[i] < thr:
        return None
    lo = i
    while lo > 0 and np.isfinite(hv[lo - 1]) and hv[lo - 1] >= thr:
        lo -= 1
    hi = i
    while hi < hv.size - 1 and np.isfinite(hv[hi + 1]) and hv[hi + 1] >= thr:
        hi += 1
    if lo == 0 or hi == hv.size - 1 or not np.isfinite(hv[lo - 1]) or not np.isfinite(hv[hi + 1]):
        return None  # half-amplitude level not reached on valid data
    return float(math.log(freq[hi] / freq[lo]))


def _peaks(doc: dict) -> list[dict]:
    fams = {f["family_id"]: f for f in doc["peak_families"]}
    out = []
    for sel in doc["selections"]:
        for p in sel["peaks"]:
            fam = fams.get(p["peak_family_id"], {})
            if (
                p.get("status") == "peak_found"
                and fam.get("plausible")
                and "possible_source_peak" not in fam.get("flags", [])
            ):
                if doc["method"] == "sdof" and p["peak_family_id"] != sel["peak_family_id"]:
                    continue
                out.append(
                    {
                        "family_id": p["peak_family_id"],
                        "fp": p["observed_peak_frequency_hz"],
                        "ap": p["observed_peak_amplitude"],
                        "selection": sel,
                    }
                )
    return out


def _time_overlap(da: dict, db: dict, sa: dict, sb: dict) -> float | None:
    """Fraction of run A's selected recording time also covered by run B's selected windows."""
    na, nb = da["recording"]["window_length_samples"], db["recording"]["window_length_samples"]
    if not sa["window_start_samples"]:
        return None
    end = max(max(sa["window_start_samples"]) + na, max(sb["window_start_samples"] or [0]) + nb)
    ma, mb = np.zeros(end, bool), np.zeros(end, bool)
    for s in sa["window_start_samples"]:
        ma[s : s + na] = True
    for s in sb["window_start_samples"]:
        mb[s : s + nb] = True
    return float((ma & mb).sum() / ma.sum())


def _curve_diff(da: dict, db: dict, sa: dict, sb: dict) -> dict:
    fa, fb = np.array(da["grid"]["frequency_hz"]), np.array(db["grid"]["frequency_hz"])
    ppd = da["grid"]["points_per_decade"]
    ia = {round(math.log10(f) * ppd): k for k, f in enumerate(fa)}
    ib = {round(math.log10(f) * ppd): k for k, f in enumerate(fb)}
    common = sorted(set(ia) & set(ib))
    fmin, fmax = da["config"]["peak_search"]["fmin_hz"], da["config"]["peak_search"]["fmax_hz"]
    ha, hb = _nan(sa["mean_curve"]["hv_geometric_mean"]), _nan(sb["mean_curve"]["hv_geometric_mean"])
    d = [
        abs(math.log(hb[ib[c]] / ha[ia[c]]))
        for c in common
        if fmin <= fa[ia[c]] <= fmax and np.isfinite(ha[ia[c]]) and np.isfinite(hb[ib[c]])
    ]
    if not d:
        return {"status": "not_evaluable", "common_valid_bins": 0}
    med = float(np.median(d))
    return {
        "status": "pass" if med <= CRITERIA["curve_max_median_abs_ln_diff"] else "fail",
        "median_abs_ln_diff": med,
        "max_abs_ln_diff": float(np.max(d)),
        "common_valid_bins": len(d),
    }


def _compare_method(da: dict, db: dict | None, reason: str | None) -> dict:
    if db is None:
        return {"status": "not_evaluable", "reason": reason or "run B unavailable"}
    if db["interpretation"]["status"] == "not_computable" or db["method_status"] == "not_computable":
        return {
            "status": "not_evaluable",
            "reason": f"run B {db['method']} not computable: {db['method_status_reason'] or db['interpretation']['evidence'][:1]}",
        }
    if da["method_status"] != "computed":
        return {"status": "not_evaluable", "reason": f"run A {da['method']} status {da['method_status']}"}
    lw_a, df_a, df_b = (
        da["recording"]["window_length_s"],
        da["recording"]["fft_resolution_hz"],
        db["recording"]["fft_resolution_hz"],
    )
    pa, pb = _peaks(da), _peaks(db)
    used_b = set()
    peaks = []
    for p in pa:
        cands = [(abs(math.log10(q["fp"] / p["fp"])), j) for j, q in enumerate(pb) if j not in used_b]
        cands = [c for c in cands if c[0] <= CRITERIA["family_match_max_abs_log10_ratio"]]
        resolved = (
            p["fp"] * lw_a >= CRITERIA["resolved_min_cycles_per_window"]
            and max(df_a, df_b) / p["fp"] <= CRITERIA["resolved_max_fft_resolution_over_fp"]
        )
        row = {
            "family_a": p["family_id"],
            "fp_a_hz": p["fp"],
            "ap_a": p["ap"],
            "adequately_resolved": bool(resolved),
            "fft_resolution_a_hz": df_a,
            "fft_resolution_b_hz": df_b,
        }
        if not cands:
            row.update(matched=False)
            peaks.append(row)
            continue
        _, j = min(cands)
        used_b.add(j)
        q = pb[j]
        fpr, apr = math.log(q["fp"] / p["fp"]), math.log(q["ap"] / p["ap"])
        fa, fb = np.array(da["grid"]["frequency_hz"]), np.array(db["grid"]["frequency_hz"])
        wa = _width_ln(fa, _nan(p["selection"]["mean_curve"]["hv_geometric_mean"]), p["fp"], p["ap"])
        wb = _width_ln(fb, _nan(q["selection"]["mean_curve"]["hv_geometric_mean"]), q["fp"], q["ap"])
        row.update(
            matched=True,
            family_b=q["family_id"],
            fp_b_hz=q["fp"],
            ap_b=q["ap"],
            fp_ln_ratio=fpr,
            ap_ln_ratio=apr,
            fp_status="pass" if abs(fpr) <= CRITERIA["fp_max_abs_ln_ratio"] else "fail",
            ap_status="pass" if abs(apr) <= CRITERIA["ap_max_abs_ln_ratio"] else "fail",
            half_amplitude_width_ln_a=wa,
            half_amplitude_width_ln_b=wb,
            curve=_curve_diff(da, db, p["selection"], q["selection"]),
            windows_a=p["selection"]["n_selected"],
            windows_b=q["selection"]["n_selected"],
            selected_time_a_also_selected_in_b=_time_overlap(da, db, p["selection"], q["selection"]),
            uncertainty_status_a=p["selection"]["uncertainty"]["status"],
            uncertainty_status_b=q["selection"]["uncertainty"]["status"],
        )
        peaks.append(row)
    unmatched_b = [pb[j]["family_id"] for j in range(len(pb)) if j not in used_b]
    issues = []
    if any(not r["matched"] for r in peaks) or unmatched_b:
        issues.append("plausible peak families differ between runs")
    evaluable = [r for r in peaks if r["matched"] and r["adequately_resolved"]]
    for r in evaluable:
        for k in ("fp_status", "ap_status"):
            if r[k] == "fail":
                issues.append(f"{r['family_a']}: {k[:2]} differs beyond tolerance")
        if r["curve"]["status"] == "fail":
            issues.append(f"{r['family_a']}: mean curves differ beyond tolerance on common frequencies")
    if issues:
        status = "sensitive"
    elif evaluable:
        status = "consistent"
    else:
        status = "not_evaluable"
        issues.append("no adequately resolved peak family common to both runs")
    return {"status": status, "issues": issues, "peaks": peaks, "unmatched_run_b_families": unmatched_b}


def compare_runs(
    docs_a: dict[str, dict],
    docs_b: dict[str, dict] | None,
    reason: str | None = None,
    planned_window_length_b_s: float | None = None,
) -> dict:
    a = docs_a["standard"]
    methods = {
        m: _compare_method(docs_a[m], None if docs_b is None else docs_b.get(m), reason)
        for m in ("standard", "sdof")
    }
    st = [v["status"] for v in methods.values()]
    if "sensitive" in st:
        status = "sensitive"
    elif "consistent" in st:
        status = "consistent"
    else:
        status = "not_evaluable"
    run_b = None if docs_b is None else docs_b["standard"]["run_id"]
    rec = {
        "schema_id": "hvsr-analyzer/robustness",
        "schema_version": SCHEMA_VERSIONS["robustness"],
        "run_a": a["run_id"],
        "run_b": run_b,
        "window_length_a_s": a["recording"]["window_length_s"],
        "window_length_b_s": planned_window_length_b_s
        if docs_b is None
        else docs_b["standard"]["recording"]["window_length_s"],
        "unchanged_settings": "smoothing b, step, physical search band, time span, component combination, response settings",
        "criteria": CRITERIA,
        "status": status,
        "methods": methods,
        "notes": NOTES + ([f"run B not evaluable: {reason}"] if reason else []),
    }
    rec["comparison_id"] = "c" + canonical_hash({"a": a["run_id"], "b": run_b, "criteria": CRITERIA})[:24]
    return rec
