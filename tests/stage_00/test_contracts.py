"""Stage 0: contracts, configuration, isolation, traceability, fixtures."""

from __future__ import annotations

import copy
import gzip
import io
import json
import subprocess
import sys
import tomllib

import numpy as np
import pytest
from pydantic import BaseModel, ValidationError

from hvsr_core.config import AnalysisConfig, AnalysisRequest, DisplayOptions, ReportOptions
from hvsr_core.docgen import config_schema_text, configuration_markdown, packaged_config_schema_text
from hvsr_core.jsonutil import canonical_dumps, sha256_bytes, strict_loads
from hvsr_core.model import iso_to_ns, ns_to_iso
from hvsr_core.schema_validation import validation_errors

SHA = "0" * 64


def structural_analysis_example() -> dict:
    """Schema-shape fixture (NOT a scientific result): tiny arrays exercising every required key."""
    sel = {
        "selection_id": "standard",
        "peak_family_id": None,
        "status": "computed",
        "status_reason": None,
        "selection_rule": "std-earliest-nonoverlap-v1",
        "selection_conditional": True,
        "n_selected": 1,
        "n_requested": None,
        "window_ids": ["w000000"],
        "window_start_samples": [0],
        "mean_curve": {
            "hv_geometric_mean": [None, None],
            "ci95_lower": [None, None],
            "ci95_upper": [None, None],
            "count": [0, 0],
            "log_std": [None, None],
        },
        "uncertainty": {
            "status": "ci_not_estimable",
            "status_reason": "structural example",
            "method": "none",
            "label": "Estimated 95% pointwise CI for the selected-window mean",
            "selection_conditional": True,
            "coverage_calibrated": False,
            "seed": None,
            "n_bootstrap": None,
        },
        "peaks": [],
        "maximum": {"maximum_amplitude": None, "maximum_frequency_hz": None},
        "templates": None,
        "sesame": {"status": "not_requested"},
    }
    return {
        "schema_id": "hvsr-analyzer/analysis",
        "schema_version": "2.0.0",
        "run_id": "example",
        "method": "standard",
        "method_status": "computed",
        "method_status_reason": None,
        "generated_utc": "2026-10-20T00:00:00Z",
        "record": {
            "record_code": None,
            "measurement_id": None,
            "metadata_revision": None,
            "latitude": None,
            "longitude": None,
            "measurement_context": "unknown",
        },
        "provenance": {
            "software_version": "0",
            "algorithm_versions": {},
            "git_commit": None,
            "dependencies": {},
            "numeric_backend": {},
            "input_hashes": {},
            "config_hash": SHA,
            "template_bank": None,
        },
        "config": AnalysisConfig().model_dump(mode="json"),
        "config_hash": SHA,
        "derived_operations": [],
        "instrument": {
            "status": "provisional_uncalibrated",
            "reasons": [],
            "flags": [],
            "correction": "none",
            "usable_band_hz": [None, None],
        },
        "recording": {
            "sampling_rate_hz": 100.0,
            "start_utc": None,
            "raw_duration_s": 0.0,
            "analyzed_start_s": 0.0,
            "analyzed_end_s": 0.0,
            "segment_count": 1,
            "window_length_samples": 5000,
            "window_length_s": 50.0,
            "step_samples": 500,
            "step_s": 5.0,
            "fft_resolution_hz": 0.02,
            "nyquist_hz": 50.0,
        },
        "grid": {
            "definition": "f_i = 10**(i/200)",
            "points_per_decade": 200,
            "frequency_hz": [1.0, 1.0116],
            "valid": [False, False],
            "invalid_reasons": {},
        },
        "windows": {"candidate_count": 1, "qc_passed_count": 1, "rejected_by_reason": {}, "candidates_url": None},
        "selections": [sel],
        "peak_families": [],
        "interpretation": {
            "status": "not_computable",
            "fundamental_frequency_candidate_hz": None,
            "evidence": [],
            "flags": [],
            "human_annotations_url": None,
        },
        "quality_dimensions": {"usability": {}, "peak_identifiability": {}, "sensitivity": {}, "summary_score": None},
        "calibration": {"status": "uncalibrated", "profile": None, "note": "x"},
        "units": {"frequency": "Hz"},
        "ci_label": "Estimated 95% pointwise CI for the selected-window mean",
        "sesame_requested": False,
    }


def structural_manifest_example() -> dict:
    art = {
        "artifact_id": "a1",
        "label": "Original file",
        "kind": "original",
        "url": "/api/measurements/x/original/a1",
        "mime_type": "application/octet-stream",
        "byte_size": 10,
        "sha256": SHA,
        "availability": "available",
        "availability_reason": None,
        "generated_by": {},
        "run_id": None,
        "method": None,
        "source_refs": [],
    }
    cats = {k: [] for k in ("original_data", "canonical_recordings", "sensor_metadata", "analysis_runs", "spectra", "plots", "templates", "provenance", "licensing", "quality_evaluation")}
    cats["original_data"] = [art]
    return {
        "schema_id": "hvsr-analyzer/manifest",
        "schema_version": "2.0.0",
        "measurement_id": "123e4567-e89b-12d3-a456-426614174000",
        "record_code": "s5f3c-2026.8000",
        "geohash": "d1s5f3c0b2kq",
        "decimal_year": 2026.8,
        "decimal_year_source": "recording_start",
        "created_utc": "2026-10-20T00:00:00Z",
        "metadata_revision": 1,
        "metadata": {},
        "original_bytes_retained": True,
        "categories": cats,
        "licensing": {
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Example Org",
            "source_citation": None,
            "consent_version": "1",
            "consent_text_sha256": SHA,
            "consent_utc": "2026-10-20T00:00:00Z",
            "rights_confirmed": True,
            "modification_history": [],
        },
        "provenance": {},
        "moderation": {"status": "public"},
    }


@pytest.mark.acceptance("AC-S0-01")
def test_schemas_accept_conforming_examples():
    assert validation_errors(structural_analysis_example(), "analysis") == []
    assert validation_errors(structural_manifest_example(), "manifest") == []
    assert validation_errors(AnalysisRequest().model_dump(mode="json"), "config") == []


@pytest.mark.acceptance("AC-S0-02")
def test_schemas_reject_malformed():
    doc = structural_analysis_example()
    bad = copy.deepcopy(doc)
    del bad["run_id"]
    assert validation_errors(bad, "analysis")
    bad = copy.deepcopy(doc)
    bad["method"] = "best_looking"
    assert validation_errors(bad, "analysis")
    bad = copy.deepcopy(doc)
    bad["selections"][0]["mean_curve"]["hv_geometric_mean"] = [float("nan"), 1.0]
    assert any("non-finite" in e for e in validation_errors(bad, "analysis"))
    bad = copy.deepcopy(doc)
    bad["method_status_reason"] = json.dumps({"nested": [1, 2]})
    assert any("JSON encoded inside a string" in e for e in validation_errors(bad, "analysis"))
    bad = copy.deepcopy(doc)
    bad["ci_label"] = "95% confidence band"
    assert validation_errors(bad, "analysis")
    man = structural_manifest_example()
    man["categories"]["plots"] = [dict(man["categories"]["original_data"][0], url="https://bucket.example/signed?X-Amz-Expires=60")]
    assert validation_errors(man, "manifest"), "expiring signed URLs must not be permanent manifest links"
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate({"config": {"windowing": {"window_length_s": -1}}})
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate({"config": {"unknown_field": 1}})
    with pytest.raises(ValueError):
        strict_loads('{"a": NaN}')
    assert canonical_dumps({"a": float("inf"), "b": float("nan")}) == '{"a":null,"b":null}'


def _walk_fields(model: type[BaseModel], prefix=""):
    for name, field in model.model_fields.items():
        yield f"{prefix}{name}", field
        ann = field.annotation
        for cand in getattr(ann, "__args__", ()) or (ann,):
            if isinstance(cand, type) and issubclass(cand, BaseModel):
                yield from _walk_fields(cand, f"{prefix}{name}.")


@pytest.mark.acceptance("AC-S0-03")
def test_no_undocumented_defaults(repo_root):
    undocumented = [p for p, f in _walk_fields(AnalysisRequest) if not (f.description or "").strip()]
    assert undocumented == []
    assert (repo_root / "docs/configuration.md").read_text() == configuration_markdown(), "run scripts/gen_docs.py"
    assert packaged_config_schema_text() == config_schema_text(), "run scripts/gen_docs.py"


@pytest.mark.acceptance("AC-S0-04")
def test_config_roundtrip_and_scientific_hash():
    req = AnalysisRequest.model_validate(
        {"config": {"windowing": {"window_length_s": 60, "step_s": 2.5}, "spectral": {"horizontal_combination": "geometric"}}}
    )
    text = canonical_dumps(req)
    again = AnalysisRequest.model_validate(json.loads(text))
    assert again == req
    assert canonical_dumps(again) == text
    h = req.config.scientific_hash()
    code = "from hvsr_core.config import AnalysisRequest;import json,sys;print(AnalysisRequest.model_validate(json.loads(sys.argv[1])).config.scientific_hash())"
    out = subprocess.run([sys.executable, "-c", code, text], capture_output=True, text=True, check=True).stdout.strip()
    assert out == h, "hash must be stable across processes"
    toggled = req.model_copy(update={"report": ReportOptions(evaluate_sesame=False), "display": DisplayOptions(dpi=300), "robustness_check": False})
    assert toggled.config.scientific_hash() == h
    changed = req.config.model_copy(update={"spectral": req.config.spectral.model_copy(update={"ko_bandwidth": 41.0})})
    assert changed.scientific_hash() != h


@pytest.mark.acceptance("AC-S0-05")
def test_core_imports_without_web_or_network():
    code = r"""
import socket, sys, os
for k in list(os.environ):
    if k.startswith(("AWS_", "R2_", "DATABASE_", "S3_")):
        del os.environ[k]
class NoNet(socket.socket):
    def connect(self, *a, **k): raise RuntimeError("network used during import")
socket.socket = NoNet
import hvsr_core, hvsr_core.config, hvsr_core.model, hvsr_core.metadata, hvsr_core.schema_validation
import hvsr_core.research.synthetic
try:
    import hvsr_core.pipeline
except ModuleNotFoundError as e:
    if "hvsr_core.pipeline" not in str(e): raise
bad = [m for m in ("fastapi", "sqlalchemy", "boto3", "botocore", "starlette", "hvsr_web", "hvsrpy", "psycopg") if m in sys.modules]
assert not bad, bad
print("ok")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.acceptance("AC-S0-06")
def test_every_stage_has_traceable_ids(repo_root):
    reqs = tomllib.loads((repo_root / "docs/acceptance/requirements.toml").read_text())["req"]
    stages = {r["stage"] for r in reqs}
    assert stages == set(range(10))
    ids = [r["id"] for r in reqs]
    assert len(ids) == len(set(ids))
    for r in reqs:
        assert r["id"].startswith(f"AC-S{r['stage']}-")
        if r.get("status") in ("BLOCKED", "UNVALIDATED"):
            assert r.get("reason") and r.get("needs"), r["id"]
        else:
            assert "status" not in r or r["status"] in ("BLOCKED", "UNVALIDATED"), r["id"]


@pytest.mark.acceptance("AC-S0-07")
def test_fixtures_known_and_redistributable(fixture_dir):
    from hvsr_core.research.fixture_specs import FIXTURES, regenerate

    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    assert "redistributable" in manifest["license"]
    for name, entry in manifest["fixtures"].items():
        payload = (fixture_dir / entry["file"]).read_bytes()
        assert sha256_bytes(payload) == entry["sha256"], name
        assert entry["known_properties"]["tf_peak_frequency_hz"] > 0
        assert entry["license"]
    # deterministic regeneration
    rec1, truth1 = regenerate("sdof2hz_elapsed_50hz")
    rec2, truth2 = regenerate("sdof2hz_elapsed_50hz")
    assert np.array_equal(rec1.segments[0].data, rec2.segments[0].data)
    assert truth1 == truth2
    assert abs(truth1["tf_peak_frequency_hz"] - 2.0 * np.sqrt(2 / (1 + np.sqrt(1 + 8 * 0.2**2)))) < 0.01
    # stored CSV contains the regenerated values (6-decimal formatting)
    text = gzip.decompress((fixture_dir / FIXTURES["sdof2hz_elapsed_50hz"]["file"]).read_bytes()).decode()
    arr = np.loadtxt(io.StringIO(text), delimiter=",", skiprows=1, max_rows=200)
    assert np.allclose(arr[:, 1], rec1.segments[0].data[0, :200], atol=5e-7)


@pytest.mark.acceptance("AC-S0-08")
def test_dependencies_locked(repo_root):
    lock = tomllib.loads((repo_root / "uv.lock").read_text())
    names = {p["name"]: p.get("version") for p in lock["package"]}
    for dep in ("numpy", "scipy", "obspy", "pydantic", "fastapi", "sqlalchemy", "arch", "jsonschema", "matplotlib"):
        assert names.get(dep), f"{dep} not pinned in uv.lock"


@pytest.mark.acceptance("AC-S0-09")
def test_timing_rules():
    assert iso_to_ns("2026-10-20T17:57:00Z") == iso_to_ns("2026-10-20T12:57:00-05:00")
    assert iso_to_ns("2026-01-01T00:00:00.000000001Z") - iso_to_ns("2026-01-01T00:00:00Z") == 1
    with pytest.raises(ValueError, match="explicit UTC offset"):
        iso_to_ns("2026-10-20T17:57:00")
    ns = iso_to_ns("2024-02-29T23:59:59.123456789Z")
    assert ns_to_iso(ns) == "2024-02-29T23:59:59.123456789Z"
    assert ns_to_iso(iso_to_ns("2026-10-20T00:00:00.250Z")) == "2026-10-20T00:00:00.25Z"
