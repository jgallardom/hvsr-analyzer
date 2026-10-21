"""The committed example exports (docs/examples, written by scripts/export_examples.py) are unedited
downloads of a real analysis: they validate, match their manifest checksums, and equal a fresh run."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from hvsr_core import exports
from hvsr_core.docgen import data_dictionary_markdown
from hvsr_core.schema_validation import validation_errors

from stage8_support import ROOT, download, entries, get_json, one, synthetic_csv_gz

EXAMPLES = ROOT / "docs" / "examples"
JSON_SCHEMA = {
    "manifest.json": "manifest",
    "analysis_sdof.json": "analysis",
    "analysis_standard.json": "analysis",
    "data_dictionary.json": "data_dictionary",
}
CSV_TABLE = {
    "mean_spectrum_sdof.csv": "mean_spectrum",
    "mean_spectrum_standard.csv": "mean_spectrum",
    "window_table_sdof.csv": "window_table_sdof",
    "window_table_standard.csv": "window_table_standard",
}
ARTIFACTS = {
    "analysis_sdof.json": ("analysis_json", "sdof"),
    "analysis_standard.json": ("analysis_json", "standard"),
    "mean_spectrum_sdof.csv": ("mean_spectrum_csv", "sdof"),
    "mean_spectrum_standard.csv": ("mean_spectrum_csv", "standard"),
    "window_table_sdof.csv": ("window_table_csv", "sdof"),
    "window_table_standard.csv": ("window_table_csv", "standard"),
    "plot_sdof.svg": ("plot_svg", "sdof"),
}


def _bytes(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


def _manifest() -> dict:
    return json.loads(_bytes("manifest.json"))


@pytest.mark.acceptance("AC-S8-13")
def test_data_dictionary_document_is_current():
    assert (ROOT / "docs" / "data-dictionary.md").read_text() == data_dictionary_markdown(), (
        "run scripts/gen_docs.py"
    )


@pytest.mark.acceptance("AC-S8-10")
def test_example_exports_validate_and_are_unedited_downloads():
    assert {p.name for p in EXAMPLES.iterdir()} == {
        "README.md",
        "manifest.json",
        "data_dictionary.json",
        *ARTIFACTS,
    }
    for name, schema in JSON_SCHEMA.items():
        assert validation_errors(json.loads(_bytes(name)), schema) == [], name
    for name, table in CSV_TABLE.items():
        assert exports.validate_csv(_bytes(name), table) == [], name
    manifest = _manifest()
    plots = [e for e in entries(manifest) if e["kind"].startswith("plot_")]
    assert plots and all(validation_errors(e["generated_by"]["recipe"], "plot_recipe") == [] for e in plots)
    for name, (kind, method) in ARTIFACTS.items():
        entry = one(entries(manifest), kind=kind, method=method)
        data = _bytes(name)
        assert hashlib.sha256(data).hexdigest() == entry["sha256"] and len(data) == entry["byte_size"], name
    assert json.loads(_bytes("data_dictionary.json")) == exports.data_dictionary()
    title = ET.fromstring(_bytes("plot_sdof.svg")).find("{http://www.w3.org/2000/svg}title")
    assert title is not None and title.text == f"{manifest['record_code']} (40.416775, -3.70379)"


@pytest.mark.acceptance("AC-S8-10")
def test_example_numbers_equal_a_fresh_analysis_of_the_same_recording(site, long_run):
    original = one(_manifest()["categories"]["original_data"], kind="original_csv_gz")
    assert original["sha256"] == hashlib.sha256(synthetic_csv_gz(1800, seed=21)).hexdigest()
    fresh = get_json(site, f"/api/v1/measurements/{long_run.measurement_id}/manifest")
    for name, (kind, method) in ARTIFACTS.items():
        if not name.endswith(".csv"):
            continue
        data, _ = download(site, one(entries(fresh), kind=kind, method=method, run_id=long_run.run_id)["url"])
        assert data == _bytes(name), name
    for method in ("sdof", "standard"):
        example = json.loads(_bytes(f"analysis_{method}.json"))
        url = one(entries(fresh), kind="analysis_json", method=method, run_id=long_run.run_id)["url"]
        doc = json.loads(download(site, url)[0])
        assert example["grid"] == doc["grid"]
        assert example["selections"] == doc["selections"], method
        assert set(example) == set(doc) and example["licensing"].keys() == doc["licensing"].keys()
        assert example["licensing"]["attribution"] == doc["licensing"]["attribution"] == "Example Observatory"
