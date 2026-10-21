"""Complete analysis runs: science (cached, immutable) -> per-method analysis.json -> reporting layer.

Dependency graph (acyclic):
  recording + scientific config -> CoreRun (FFT, QC, Standard, discovery)
                                 -> SDOF matching/selection per plausible family
                                 -> uncertainty (bootstrap on cached spectra)
                                 => scientific documents (cached by science key)
  scientific documents + ReportOptions (SESAME) + robustness record => published documents
Reporting/display changes never recompute spectra or bootstrap replicates.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

import numpy as np

from . import sesame
from .config import AnalysisConfig, AnalysisRequest, ReportOptions, robustness_config
from .core import CoreRun, run_core
from .curves import MeanCurve, geometric_mean_curve
from .exports import science_supplement
from .jsonutil import canonical_dumps, canonical_hash, strict_loads, to_jsonable
from .matching import FamilySelection, sdof_for_families
from .model import Recording, ns_to_iso
from .peaks import Family, parabolic_peak, strict_max_in_band
from .selection import search_band_mask
from .templates import TemplateBank, resolve_sdof_bank
from .uncertainty import (
    LABEL,
    SelectionUncertainty,
    export_curve,
    peak_replicates,
    selection_uncertainty,
    window_ln_peak,
)
from .versions import ALGORITHM_VERSIONS, PACKAGE_VERSION, SCHEMA_VERSIONS

TOLERANCE_POLICY = {
    "float_rtol": 1e-9,
    "float_atol": 1e-12,
    "selections": "identical window ids and order",
    "bootstrap": "same seed; derived bounds within the float tolerance",
    "scope": "same platform and dependency lock; cross-platform bitwise identity is not promised",
}
VALIDATION_STATUS = {
    "protocol_id": "hvsr-mc-validation-v1",
    "protocol_sha256": "8768c5c50ece353b476b51915ed4038d6541026f3481dbf93d30d614333dc985",
    "verdict": "not_met",
    "report": "docs/validation/report-v1.md",
}
CALIBRATION_NOTE = (
    "Nominal 95 %, not calibrated. The locked Monte Carlo validation (hvsr-mc-validation-v1) did not meet its "
    "coverage targets: coverage was about 75 % under minute-scale H/V regime changes, and strongly low-frequency-"
    "dominated records produced no CI because the default anti-trigger rejects nearly every window. "
    "See docs/validation/report-v1.md."
)
UNITS = {
    "frequency": "Hz",
    "hv": "dimensionless amplitude ratio (H/V)",
    "time": "s",
    "log_std": "natural-log units of H/V",
    "ci": "same units as hv; bounds of the selected-window geometric mean",
    "window_start_samples": "samples from the first canonical sample",
}
SELECTION_RULES = {
    "standard": "basic QC, then earliest-first non-overlapping windows (no shape preference)",
    "standard_basic_audit": "basic QC, then earliest-first non-overlapping windows; no frequency-outlier rejection (audit)",
    "sdof": "SDOF R2_log ranking on the 129-point peak-aligned q grid inside the family; global greedy non-overlapping selection; unweighted geometric mean of ORIGINAL spectra",
}


def recording_hash(rec: Recording) -> str:
    h = hashlib.sha256()
    h.update(
        canonical_dumps(
            {"fs": rec.sampling_rate_hz, "t0": rec.t0_utc_ns, "channels": [vars(c) for c in rec.channels]}
        ).encode()
    )
    for seg in rec.segments:
        h.update(np.int64(seg.start_index).tobytes())
        h.update(np.ascontiguousarray(seg.data, dtype=np.float64).tobytes())
    return h.hexdigest()


@dataclass
class RecordInfo:
    record_code: str | None = None
    measurement_id: str | None = None
    metadata_revision: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    measurement_context: str = "unknown"
    input_hashes: dict = field(default_factory=dict)
    response_sha256: str | None = None
    instrument: dict | None = None

    def scientific(self) -> dict:
        return {
            "context": self.measurement_context,
            "response_sha256": self.response_sha256,
            "instrument": self.instrument,
        }

    def block(self) -> dict:
        return {
            "record_code": self.record_code,
            "measurement_id": self.measurement_id,
            "metadata_revision": self.metadata_revision,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "measurement_context": self.measurement_context,
        }


def science_key(rec: Recording, config: AnalysisConfig, info: RecordInfo) -> str:
    bank, _ = resolve_sdof_bank(config.sdof.template_bank, config.spectral.ko_bandwidth)
    return canonical_hash(
        {
            "recording": recording_hash(rec),
            "config": config.scientific_hash(),
            "record_science": info.scientific(),
            "record": info.block(),  # documents embed it; a run belongs to one measurement snapshot
            "algorithms": ALGORITHM_VERSIONS,
            "package": PACKAGE_VERSION,
            "bank": bank.manifest["data_sha256"],
        }
    )


def _git() -> dict:
    commit = os.environ.get("HVSR_GIT_COMMIT")
    dirty = None
    exe = shutil.which("git")
    if commit is None and exe is not None:
        root = Path(__file__).resolve().parents[4]
        try:
            run = {"cwd": root, "capture_output": True, "text": True, "check": True, "timeout": 5}
            commit = subprocess.run([exe, "rev-parse", "HEAD"], **run).stdout.strip()  # noqa: S603
            status = subprocess.run([exe, "status", "--porcelain", "--untracked-files=no"], **run)  # noqa: S603
            dirty = bool(status.stdout.strip())
        except (subprocess.SubprocessError, OSError):
            commit = None
    return {"git_commit": commit, "git_dirty": dirty}


def _dependencies() -> dict:
    out = {}
    for name in ("numpy", "scipy", "arch", "pydantic", "obspy", "jsonschema"):
        try:
            out[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            continue
    return out


def _numeric_backend() -> dict:
    return {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "numpy": np.__version__,
    }


@dataclass
class SelectionResult:
    method: str
    family: Family | None
    rows: np.ndarray  # selection order (greedy order for SDOF, time order for Standard)
    mean: MeanCurve
    unc: SelectionUncertainty
    status: str
    status_reason: str | None
    requested_n: int | None = None
    sdof: FamilySelection | None = None
    extra_peaks: list[dict] = field(default_factory=list)
    window_peaks_hz: dict = field(default_factory=dict)  # family_id -> list aligned with time-sorted rows


@dataclass
class Science:
    key: str
    run_id: str
    generated_utc: str
    config: AnalysisConfig
    info: RecordInfo
    rec: dict
    core: CoreRun
    bank: TemplateBank
    bank_warnings: list[str]
    standard: SelectionResult
    audit: SelectionResult | None
    sdof: list[SelectionResult]


def _peak_dict(
    sd, mean: MeanCurve, rows_sorted: np.ndarray, fam: Family, families: list[Family], reps, band
) -> tuple[dict, list]:
    ln_f = np.log(sd.grid.freq)
    i = strict_max_in_band(mean.ln_mean, mean.valid, fam.lo_index, fam.hi_index)
    d: dict = {"peak_family_id": fam.family_id}
    if i is None:
        d.update(
            status="no_peak_in_family_band", observed_peak_frequency_hz=None, observed_peak_amplitude=None
        )
    else:
        f, a = parabolic_peak(ln_f, mean.ln_mean, i)
        d.update(
            status="peak_found",
            observed_peak_frequency_hz=float(f),
            observed_peak_amplitude=float(math.exp(a)),
        )
    wl = window_ln_peak(sd, rows_sorted, fam.lo_index, fam.hi_index)
    fin = wl[np.isfinite(wl)]
    d["window_peak_frequency_hz"] = {
        "geometric_mean": float(math.exp(fin.mean())) if fin.size else None,
        "log_std": float(np.std(fin, ddof=1)) if fin.size >= 2 else None,
        "n_windows_with_peak": int(fin.size),
        "n_windows": int(rows_sorted.size),
        "note": "dispersion of per-window peaks; not a confidence interval",
    }
    others = [(f.family_id, f.lo_index, f.hi_index) for f in families if f.family_id != fam.family_id]
    d["bootstrap"] = (
        None
        if reps is None
        else peak_replicates(
            reps, mean.valid, ln_f, (fam.family_id, fam.lo_index, fam.hi_index), others, band
        )
    )
    return d, [None if not np.isfinite(v) else float(math.exp(v)) for v in wl]


def _selection(
    method,
    sd,
    prep,
    config,
    rows,
    mean,
    fam,
    families,
    band,
    status,
    reason,
    plaus,
    requested=None,
    sdof=None,
) -> SelectionResult:
    rows_sorted = np.sort(np.asarray(rows, dtype=np.int64))
    unc = selection_uncertainty(sd, prep, rows_sorted, mean, config, band, fam, families)
    res = SelectionResult(
        method, fam, np.asarray(rows, dtype=np.int64), mean, unc, status, reason, requested, sdof
    )
    if fam is not None:
        _, res.window_peaks_hz[fam.family_id] = _peak_dict(
            sd, mean, rows_sorted, fam, families, unc.ci.replicates, band
        )
    for other in plaus:
        if fam is not None and other.family_id == fam.family_id:
            continue
        d, wp = _peak_dict(sd, mean, rows_sorted, other, families, unc.ci.replicates, band)
        res.extra_peaks.append(d)
        res.window_peaks_hz[other.family_id] = wp
    return res


def compute_science(rec: Recording, config: AnalysisConfig, info: RecordInfo | None = None) -> Science:
    info = info or RecordInfo()
    key = science_key(rec, config, info)
    core = run_core(rec, config, info.instrument, info.measurement_context)
    sd, disc = core.spectra, core.discovery
    band = search_band_mask(sd, config)
    fams = disc.families
    plaus = [f for f in fams if f.plausible]
    primary = next((f for f in fams if f.family_id == disc.fundamental_family_id), None)
    if primary is None:
        primary = next(
            (f for f in plaus if "possible_source_peak" not in f.flags), plaus[0] if plaus else None
        )
    nc = disc.status == "not_computable"
    std = _selection(
        "standard", sd, core.prep, config, core.standard.rows, disc.mean, primary, fams, band,
        "not_computable" if nc else "computed", disc.evidence[0] if nc and disc.evidence else None, plaus,
    )  # fmt: skip
    audit = None
    if core.standard.fdwra is not None:
        basic = core.standard.basic_rows
        audit = _selection(
            "standard_basic_audit", sd, core.prep, config, basic, geometric_mean_curve(sd, basic), primary, fams, band,
            "computed", None, plaus,
        )  # fmt: skip
    bank, bank_warnings = resolve_sdof_bank(config.sdof.template_bank, config.spectral.ko_bandwidth)
    sdof = []
    for sel in sdof_for_families(sd, core.qc.passed, fams, bank, config):
        fam = next(f for f in fams if f.family_id == sel.family_id)
        status = {"ok": "computed"}.get(sel.status, sel.status)
        reason = "; ".join(sel.notes) or None
        if sel.selected.size == 0:
            reason = reason or "no eligible window in this family (see eligibility counts)"
        sdof.append(
            _selection(
                "sdof",
                sd,
                core.prep,
                config,
                sel.selected,
                sel.mean,
                fam,
                fams,
                band,
                status,
                reason,
                [fam],
                sel.requested_n,
                sel,
            )
        )
    rec_meta = {
        "recording_content_sha256": recording_hash(rec),
        "sampling_rate_hz": rec.sampling_rate_hz,
        "start_utc": ns_to_iso(rec.t0_utc_ns),
        "raw_duration_s": rec.duration_s,
        "segment_count": len(rec.segments),
        "gaps": len(rec.diagnostics.gaps),
        "clipped_samples": dict(rec.diagnostics.clipped_samples),
        "warnings": list(rec.diagnostics.warnings),
    }
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Science(
        key, "r" + key[:24], now, config, info, rec_meta, core, bank, bank_warnings, std, audit, sdof
    )


# ------------------------------------------------------------------------------------------- documents
def _arr(x) -> list:
    return to_jsonable(np.asarray(x, dtype=float))


def _selection_doc(sci: Science, s: SelectionResult) -> dict:
    sd = sci.core.spectra
    win = sd.window
    ids = win.ids()
    band = search_band_mask(sd, sci.config)
    m = band & s.mean.valid
    if m.any():
        i = int(np.nanargmax(np.where(m, s.mean.ln_mean, -np.inf)))
        maximum = {"maximum_amplitude": float(s.mean.hv[i]), "maximum_frequency_hz": float(sd.grid.freq[i])}
    else:
        maximum = {"maximum_amplitude": None, "maximum_frequency_hz": None}
    peaks = []
    if s.family is not None:
        peaks.append(dict(s.unc.peak))
    peaks += s.extra_peaks
    rows_sorted = np.sort(s.rows)
    doc = {
        "selection_id": f"{s.method}-{s.family.family_id if (s.method == 'sdof' and s.family) else 'all'}",
        "peak_family_id": s.family.family_id if s.family is not None else None,
        "status": s.status,
        "status_reason": s.status_reason,
        "selection_rule": SELECTION_RULES[s.method],
        "selection_conditional": True,
        "n_selected": int(s.rows.size),
        "n_requested": s.requested_n,
        "window_ids": [ids[r] for r in s.rows],
        "window_start_samples": [int(win.global_start[r]) for r in s.rows],
        "window_peaks_hz": {k: v for k, v in s.window_peaks_hz.items()},
        "window_peaks_order": "time-sorted window_ids",
        "mean_curve": export_curve(s.mean, s.unc.ci),
        "uncertainty": to_jsonable(s.unc.ci.info),
        "window_dispersion": s.unc.dispersion,
        "peaks": to_jsonable(peaks),
        "maximum": maximum,
        "templates": None,
        "sesame": sesame.not_requested(),
    }
    doc["time_sorted_window_ids"] = [ids[r] for r in rows_sorted]
    if s.sdof is not None:
        t = s.sdof.table
        pos = {int(r): i for i, r in enumerate(t.rows)}
        sel_items = []
        for r in s.rows:
            k = pos[int(r)]
            bk = int(t.best_k[k])
            sel_items.append(
                {
                    "window_id": ids[r],
                    "r2_log": float(t.best_r2[k]),
                    "best_template": sci.bank.template_label(bk) if bk >= 0 else None,
                    "fp_hz": float(t.peaks.fp[k]),
                    "ap": float(math.exp(t.peaks.ln_ap[k])),
                    "fit_band_hz": [float(v) for v in t.fit_band_hz[k]],
                    "flags": list(t.flags[k]),
                }
            )
        doc["templates"] = {
            "bank": sci.bank.provenance(),
            "bank_warnings": list(sci.bank_warnings),
            "q_grid": "x = (i - 64) ln 2 / 64, i = 0..128 (ln(f/fp)), fit band [fp/2, 2 fp]",
            "score": "R2_log = 1 - SSE/SST in ln amplitude on the q grid (template resemblance, not a quality probability)",
            "eligibility_counts": t.status_counts(),
            "ranking_order": "descending R2_log, then earlier start, then window id",
            "selected_in_greedy_order": sel_items,
        }
    return doc


def _quality(sci: Science) -> dict:
    core = sci.core
    sd = core.spectra
    band = search_band_mask(sd, sci.config)
    disc = core.discovery
    std = sci.standard
    qc_counts = core.qc.counts()
    usability = {
        "valid_channels": 3,
        "instrument_status": core.instrument["status"],
        "duration_s": sci.rec["raw_duration_s"],
        "segment_count": sci.rec["segment_count"],
        "gaps": sci.rec["gaps"],
        "clipped_samples": sci.rec["clipped_samples"],
        "candidate_windows": int(core.window.count),
        "qc_rejected_by_reason": qc_counts,
        "qc_passed_fraction": float(core.qc.passed.mean()) if core.window.count else 0.0,
        "non_overlapping_windows": int(std.rows.size),
        "search_band_valid_fraction": float(
            (band & sd.grid.valid).sum()
            / max(
                1,
                (
                    (sd.grid.freq >= sci.config.peak_search.fmin_hz)
                    & (sd.grid.freq <= sci.config.peak_search.fmax_hz)
                ).sum(),
            )
        ),
        "temporal_groups": std.unc.ci.info.get("informative_blocks"),
        "warnings": list(core.warnings),
    }
    ident = []
    plaus = [f for f in disc.families if f.plausible]
    for f in disc.families:
        wp = std.window_peaks_hz.get(f.family_id)
        fin = np.log([v for v in (wp or []) if v is not None])
        ident.append(
            {
                "family_id": f.family_id,
                "plausible": f.plausible,
                "mean_curve_prominence_ratio": f.mean_prominence_ratio,
                "recurrence": f.recurrence,
                "window_fp_log_std": float(np.std(fin, ddof=1)) if fin.size >= 2 else None,
                "log_std_at_mean_peak": None
                if f.mean_peak_index is None
                else to_jsonable(float(disc.mean.log_std[f.mean_peak_index])),
                "possible_source_peak": "possible_source_peak" in f.flags,
                "competing_plausible_families": len(plaus) - (1 if f.plausible else 0),
            }
        )
    std_fp = {
        p["peak_family_id"]: p.get("observed_peak_frequency_hz")
        for p in [std.unc.peak, *std.extra_peaks]
        if p.get("peak_family_id")
    }
    comp = []
    for s in sci.sdof:
        a, b = std_fp.get(s.family.family_id), s.unc.peak.get("observed_peak_frequency_hz")
        comp.append(
            {
                "family_id": s.family.family_id,
                "standard_fp_hz": a,
                "sdof_fp_hz": b,
                "ln_ratio": None if (a is None or b is None) else math.log(b / a),
            }
        )
    rows = np.sort(std.rows)
    half = rows.size // 2
    tg = None
    if std.family is not None and half >= 3:
        fam = std.family
        g = []
        for part in (rows[:half], rows[half:]):
            mc = geometric_mean_curve(sd, part)
            i = strict_max_in_band(mc.ln_mean, mc.valid, fam.lo_index, fam.hi_index)
            g.append(None if i is None else parabolic_peak(np.log(sd.grid.freq), mc.ln_mean, i)[0])
        tg = {
            "family_id": fam.family_id,
            "first_half_fp_hz": g[0],
            "second_half_fp_hz": g[1],
            "ln_ratio": None if None in g else math.log(g[1] / g[0]),
            "note": "halves of the same record are not independent measurements",
        }
    sensitivity = {
        "standard_vs_sdof": comp,
        "time_groups": tg,
        "window_count": int(std.rows.size),
        "robustness": {"status": "not_run"},
    }
    return {
        "usability": usability,
        "peak_identifiability": {"families": ident},
        "sensitivity": sensitivity,
        "summary_score": None,
        "summary_score_reason": "No summary score is computed: dimensions are reported separately (D-061).",
    }


def _method_doc(
    sci: Science, method: str, selections: list[SelectionResult], status: str, reason: str | None
) -> dict:
    core = sci.core
    sd = core.spectra
    win = core.window
    fs = core.prep.fs
    git = _git()
    bank = sci.bank.provenance() if method == "sdof" else None
    return {
        "schema_id": "hvsr-analyzer/analysis",
        "schema_version": SCHEMA_VERSIONS["analysis"],
        "run_id": sci.run_id,
        "method": method,
        "method_status": status,
        "method_status_reason": reason,
        "generated_utc": sci.generated_utc,
        "record": sci.info.block(),
        "provenance": {
            "software_version": PACKAGE_VERSION,
            "algorithm_versions": dict(ALGORITHM_VERSIONS),
            "git_commit": git["git_commit"],
            "git_dirty": git["git_dirty"],
            "container_image": os.environ.get("HVSR_CONTAINER_IMAGE"),
            "dependencies": _dependencies(),
            "numeric_backend": _numeric_backend(),
            "input_hashes": {
                "recording_content_sha256": sci.rec["recording_content_sha256"],
                **sci.info.input_hashes,
            },
            "response_sha256": sci.info.response_sha256,
            "config_hash": sci.config.scientific_hash(),
            "science_key": sci.key,
            "template_bank": bank,
            "tolerance_policy": TOLERANCE_POLICY,
        },
        "config": sci.config.model_dump(mode="json"),
        "config_hash": sci.config.scientific_hash(),
        "derived_operations": core.prep.derived_operations,
        "instrument": to_jsonable(core.instrument),
        "recording": {
            "sampling_rate_hz": fs,
            "start_utc": sci.rec["start_utc"],
            "raw_duration_s": sci.rec["raw_duration_s"],
            "analyzed_start_s": core.prep.analyzed_start_index / fs,
            "analyzed_end_s": core.prep.analyzed_stop_index / fs,
            "segment_count": sci.rec["segment_count"],
            "window_length_samples": int(win.n_samples),
            "window_length_s": win.n_samples / fs,
            "step_samples": int(win.step_samples),
            "step_s": win.step_samples / fs,
            "fft_resolution_hz": float(sd.df_hz),
            "nyquist_hz": fs / 2,
        },
        "grid": {
            "definition": f"f_i = 10^(i/{sd.grid.points_per_decade}) Hz on the anchored logarithmic grid",
            "points_per_decade": int(sd.grid.points_per_decade),
            "frequency_hz": _arr(sd.grid.freq),
            "valid": [bool(v) for v in sd.grid.valid],
            "invalid_reasons": sd.grid.reason_counts(),
            "smoothing_effective_bins": _arr(sd.grid.effective_bins),
        },
        "windows": {
            "candidate_count": int(win.count),
            "qc_passed_count": int(core.qc.passed.sum()),
            "rejected_by_reason": core.qc.counts(),
            "candidates_url": None,
        },
        "selections": [_selection_doc(sci, s) for s in selections],
        "peak_families": to_jsonable([f.as_dict() for f in core.discovery.families]),
        "interpretation": {**to_jsonable(core.discovery.interpretation()), "human_annotations_url": None},
        "quality_dimensions": to_jsonable(_quality(sci)),
        "calibration": {
            "status": "uncalibrated",
            "profile": None,
            "note": CALIBRATION_NOTE,
            "validation": VALIDATION_STATUS,
        },
        "units": UNITS,
        "ci_label": LABEL,
        "sesame_requested": False,
        "robustness_comparison_url": None,
        "licensing": None,
        "external": False,
    }


def science_docs(sci: Science) -> dict[str, dict]:
    core = sci.core
    docs = {
        "standard": _method_doc(
            sci,
            "standard",
            [sci.standard],
            sci.standard.status if sci.standard.status == "not_computable" else "computed",
            sci.standard.status_reason,
        )
    }
    if sci.audit is not None:
        docs["standard_basic_audit"] = _method_doc(sci, "standard_basic_audit", [sci.audit], "computed", None)
    plaus = [f for f in core.discovery.families if f.plausible]
    if not plaus:
        st, why = (
            "not_computable",
            "no plausible peak family: SDOF matching is only performed inside plausible families",
        )
    elif any(s.rows.size for s in sci.sdof):
        st, why = "computed", None
    else:
        st, why = "no_eligible_windows", "no family has an eligible window"
    docs["sdof"] = _method_doc(sci, "sdof", sci.sdof, st, why)
    return docs


# ------------------------------------------------------------------------------------------ reporting
def apply_report(
    docs: dict[str, dict],
    report: ReportOptions,
    robustness: dict | None = None,
    comparison_url: str | None = None,
) -> dict[str, dict]:
    """Pure reporting layer: never recomputes spectra, selections, or bootstrap replicates."""
    out = copy.deepcopy(docs)
    for doc in out.values():
        doc["sesame_requested"] = bool(report.evaluate_sesame)
        doc["robustness_comparison_url"] = comparison_url
        if robustness is not None:
            doc["quality_dimensions"]["sensitivity"]["robustness"] = {
                "status": robustness["status"],
                "comparison_id": robustness["comparison_id"],
            }
        for sel in doc["selections"]:
            sel["sesame"] = (
                _sesame_for_selection(doc, sel) if report.evaluate_sesame else sesame.not_requested()
            )
    return out


def _nan(v):
    return np.array([np.nan if x is None else x for x in v], dtype=float)


def _sesame_for_selection(doc: dict, sel: dict) -> dict:
    fams = {f["family_id"]: f for f in doc["peak_families"]}
    found = [
        p
        for p in sel["peaks"]
        if p.get("status") == "peak_found" and fams.get(p["peak_family_id"], {}).get("plausible")
    ]
    if doc["interpretation"]["status"] == "not_computable" or not found:
        return sesame.no_defensible_peak(
            "no plausible peak family with a peak in this selection's mean curve"
        )
    freq = np.array(doc["grid"]["frequency_hz"], float)
    hv = _nan(sel["mean_curve"]["hv_geometric_mean"])
    ls = _nan(sel["mean_curve"]["log_std"])
    valid = np.isfinite(hv)
    out = []
    for p in found:
        f = fams[p["peak_family_id"]]
        r = sesame.evaluate_peak(
            f0=p["observed_peak_frequency_hz"],
            a0=p["observed_peak_amplitude"],
            freq=freq,
            hv=hv,
            log_std=ls,
            valid=valid,
            family_lo_hz=f["lower_bound_hz"],
            family_hi_hz=f["upper_bound_hz"],
            window_peaks_hz=sel["window_peaks_hz"].get(p["peak_family_id"], []),
            lw_s=doc["recording"]["window_length_s"],
            nw=sel["n_selected"],
        )
        r["peak_family_id"] = p["peak_family_id"]
        out.append(r)
    return {
        "status": "evaluated",
        "source": sesame.SESAME_SOURCE,
        "peaks": to_jsonable(out),
        "note": "SESAME sigma_A is window dispersion, not the plotted CI.",
    }


# --------------------------------------------------------------------------------------- cache/replay
class AnalysisCache:
    """Immutable scientific documents and their export supplement keyed by science key; optional
    directory persistence (JSON only)."""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory else None
        self._mem: dict[str, dict] = {}

    def _read(self, name: str) -> dict | None:
        if name in self._mem:
            return self._mem[name]
        if self.directory is not None:
            p = self.directory / f"{name}.json"
            if p.exists():
                self._mem[name] = strict_loads(p.read_text())
                return self._mem[name]
        return None

    def _write(self, name: str, doc: dict) -> None:
        if name in self._mem or (self.directory is not None and (self.directory / f"{name}.json").exists()):
            return  # immutable: first result wins
        self._mem[name] = doc
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self.directory / f".{name}.tmp"
            tmp.write_text(canonical_dumps(doc))
            tmp.replace(self.directory / f"{name}.json")

    def get(self, key: str) -> dict | None:
        return self._read(key)

    def put(self, key: str, docs: dict) -> None:
        self._write(key, docs)

    def get_supplement(self, key: str) -> dict | None:
        return self._read(f"{key}.supplement")

    def put_supplement(self, key: str, supplement: dict) -> None:
        self._write(f"{key}.supplement", supplement)


@dataclass
class AnalysisResult:
    key: str
    run_id: str
    scientific: dict[str, dict]
    published: dict[str, dict]
    cached: bool
    robustness: dict | None = None
    run_b: AnalysisResult | None = None
    supplement: dict | None = None


def _science_or_cache(
    rec: Recording, config: AnalysisConfig, info: RecordInfo, cache: AnalysisCache | None
) -> tuple[str, dict, dict, bool]:
    key = science_key(rec, config, info)
    if cache is not None:
        hit, sup = cache.get(key), cache.get_supplement(key)
        if hit is not None and sup is not None:
            return key, hit, sup, True
    sci = compute_science(rec, config, info)
    docs = strict_loads(canonical_dumps(to_jsonable(science_docs(sci))))
    sup = strict_loads(canonical_dumps(to_jsonable(science_supplement(sci))))
    if cache is not None:
        cache.put(key, docs)
        cache.put_supplement(key, sup)
        docs = cache.get(key)  # first stored documents win (generated_utc differs between computations)
        sup = cache.get_supplement(key)
    return key, docs, sup, False


def run_analysis(
    rec: Recording,
    request: AnalysisRequest,
    info: RecordInfo | None = None,
    cache: AnalysisCache | None = None,
) -> AnalysisResult:
    from .robustness import compare_runs

    info = info or RecordInfo()
    key, docs, sup, cached = _science_or_cache(rec, request.config, info, cache)
    comparison = run_b = None
    url = None
    if request.robustness_check:
        cfg_b = robustness_config(request.config, rec.sampling_rate_hz)
        try:
            key_b, docs_b, sup_b, cached_b = _science_or_cache(rec, cfg_b, info, cache)
            run_b = AnalysisResult(
                key_b,
                docs_b["standard"]["run_id"],
                docs_b,
                apply_report(docs_b, request.report),
                cached_b,
                supplement=sup_b,
            )
            comparison = compare_runs(docs, docs_b)
        except ValueError as exc:  # e.g. no 1.5 x Lw window fits
            comparison = compare_runs(docs, None, str(exc), cfg_b.windowing.window_length_s)
        url = f"/api/v1/comparisons/{comparison['comparison_id']}"
    published = apply_report(docs, request.report, comparison, url)
    return AnalysisResult(key, docs["standard"]["run_id"], docs, published, cached, comparison, run_b, sup)


def _walk_compare(a, b, path: str, out: list, rtol: float, atol: float) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k == "generated_utc":
                continue
            if k not in a or k not in b:
                out.append({"path": f"{path}.{k}", "issue": "missing"})
                continue
            _walk_compare(a[k], b[k], f"{path}.{k}", out, rtol, atol)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append({"path": path, "issue": f"length {len(a)} != {len(b)}"})
            return
        if (
            a
            and all(isinstance(x, (int, float)) or x is None for x in a + b)
            and any(isinstance(x, float) for x in a + b)
        ):
            x, y = _nan(a), _nan(b)
            if not np.array_equal(np.isnan(x), np.isnan(y)) or not np.allclose(
                x, y, rtol=rtol, atol=atol, equal_nan=True
            ):
                diff = np.nanmax(np.abs(x - y)) if np.any(np.isfinite(x - y)) else float("nan")
                out.append({"path": path, "issue": "numeric difference", "max_abs_diff": float(diff)})
            return
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _walk_compare(x, y, f"{path}[{i}]", out, rtol, atol)
    elif isinstance(a, float) or isinstance(b, float):
        if a is None or b is None or not math.isclose(a, b, rel_tol=rtol, abs_tol=atol):
            out.append({"path": path, "issue": "numeric difference", "a": a, "b": b})
    elif a != b:
        out.append({"path": path, "issue": "value differs", "a": a, "b": b})


def verify_replay(
    rec: Recording,
    request: AnalysisRequest,
    cached_scientific: dict[str, dict],
    info: RecordInfo | None = None,
) -> dict:
    """Explicit verification rerun: bypasses the cache, recomputes, and compares with the stored result.

    Scientific content is compared within the tolerance policy; provenance (code, dependency, and
    environment identity) is compared exactly and reported separately.
    """
    info = info or RecordInfo()
    _, fresh, _, _ = _science_or_cache(rec, request.config, info, None)

    def split(docs: dict) -> tuple[dict, dict]:
        science = {m: {k: v for k, v in d.items() if k != "provenance"} for m, d in docs.items()}
        return science, {m: d.get("provenance") for m, d in docs.items()}

    sci_a, env_a = split(cached_scientific)
    sci_b, env_b = split(fresh)
    diffs: list = []
    _walk_compare(sci_a, sci_b, "$", diffs, TOLERANCE_POLICY["float_rtol"], TOLERANCE_POLICY["float_atol"])
    env: list = []
    _walk_compare(env_a, env_b, "$", env, 0.0, 0.0)
    return {
        "status": "reproduced" if not diffs else "differs",
        "differences": diffs[:50],
        "n_differences": len(diffs),
        "environment_differences": env[:50],
        "tolerance_policy": TOLERANCE_POLICY,
        "recomputed": True,
        "run_id": fresh["standard"]["run_id"],
    }
