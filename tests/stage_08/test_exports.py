"""Independent exports: numeric identity (AC-S8-02), schemas (AC-S8-10), CSV contract (AC-S8-13)."""

from __future__ import annotations

import gzip
import json
import math
import re

import numpy as np
import pytest
from matplotlib import rc_context

from hvsr_core import exports, plots
from hvsr_core.config import DisplayOptions
from hvsr_core.schema_validation import SCHEMA_FILES, load_schema, validation_errors
from hvsr_web import runs

from stage8_support import (
    MSEED,
    as_float,
    csv_rows,
    download,
    entries,
    fixture_bytes,
    get_json,
    one,
    same,
    submit,
)

SERIES = ("frequency_hz", "hv_geometric_mean", "ci95_lower", "ci95_upper")


def _run_items(site, run_id):
    run = get_json(site, f"/api/v1/runs/{run_id}")
    return run, {m: v["artifacts"] for m, v in run["methods"].items()}


def _doc(site, items):
    data, _ = download(site, one(items, kind="analysis_json")["url"])
    return json.loads(data), data


def _csv_series(rows, selection_id):
    picked = [r for r in rows if r["selection_id"] == selection_id]
    return {k: np.array([as_float(r[k]) for r in picked]) for k in SERIES}


@pytest.mark.acceptance("AC-S8-02")
@pytest.mark.parametrize("which", ["short", "long"])
def test_plot_csv_and_json_carry_identical_numbers(site, short_run, long_run, which):
    pub = short_run if which == "short" else long_run
    run_ids = [pub.run_id] + ([pub.run_b_id] if pub.run_b_id else [])
    for run_id in run_ids:
        run, per_method = _run_items(site, run_id)
        for method, items in per_method.items():
            doc, _ = _doc(site, items)
            # the CSV holds every selection; the figure shows the main one
            rows = csv_rows(download(site, one(items, kind="mean_spectrum_csv")["url"])[0])
            for sel in doc["selections"]:
                mc, csv_series = sel["mean_curve"], _csv_series(rows, sel["selection_id"])
                assert same(csv_series["frequency_hz"], doc["grid"]["frequency_hz"])
                for k in SERIES[1:]:
                    json_vals = [math.nan if v is None else v for v in mc[k]]
                    assert same(csv_series[k], json_vals), (run_id, method, sel["selection_id"], k)
            for kind in ("plot_svg", "plot_png", "plot_svg_compact"):
                art = one(items, kind=kind)
                recipe = art["generated_by"]["recipe"]
                sel = plots.main_selection(doc, recipe["selection_id"])
                json_series = plots.plotted_series(doc, sel)
                assert recipe["series_sha256"] == plots.series_sha256(json_series)
                assert plots.series_sha256(_csv_series(rows, sel["selection_id"])) == recipe["series_sha256"]
                # the figure's own artists hold exactly these arrays
                with rc_context(plots.RC):
                    fig, handed = plots.build_figure(doc, recipe, alternate_doc=None)
                mean = next(ln for ln in fig.axes[0].lines if ln.get_gid() == "mean-curve")
                assert same(mean.get_xdata(), json_series["frequency_hz"])
                assert same(mean.get_ydata(), json_series["hv_geometric_mean"])
                for k in SERIES:
                    assert same(handed[k], json_series[k])
                # the stored image is exactly what that recipe renders from the published JSON
                image, resp = download(site, art["url"])
                alt = recipe["alternate_method"]
                alt_doc = _doc(site, per_method[alt])[0] if alt else None
                assert plots.render(doc, recipe, alternate_doc=alt_doc) == image, (run_id, method, kind)
                assert recipe["series_sha256"].encode() in image
                assert resp.headers["content-type"].startswith(art["mime_type"])


@pytest.mark.acceptance("AC-S8-02")
def test_cosmetic_rerender_is_a_new_plot_of_the_same_numbers(fresh, monkeypatch):
    from stage8_support import CSV_GZ, analyze

    site = fresh
    sub = submit(site, [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")], "rerender-0001")
    run_id = analyze(site, sub["measurement_id"], {"robustness_check": False})
    run, per_method = _run_items(site, run_id)
    published = one(per_method["sdof"], kind="plot_svg")
    body = {"method": "sdof", "display": {"show_individual_windows": True, "show_alternate_method": True}}
    assert site.client.post(f"/api/v1/runs/{run_id}/plots", json=body).status_code == 401

    # windows requested before the per-window spectra exist: explicit 409 pointing at them
    r = site.client.post(f"/api/v1/runs/{run_id}/plots", json=body, headers=site.auth)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "window_spectra_not_stored" and d["availability"] == "on_demand"
    assert d["availability_reason"]
    page = site.client.get(f"/runs/{run_id}?method=sdof&windows=true")
    assert page.status_code == 200 and f'href="{d["artifact_url"]}"' in page.text
    assert site.client.get(d["artifact_url"]).status_code == 202
    download(site, d["artifact_url"])

    # from here on nothing scientific may run: no FFT, no core run, no bootstrap
    def forbidden(*_a, **_k):
        raise AssertionError("a cosmetic change ran scientific code")

    import hvsr_core.analysis
    import hvsr_core.core
    import hvsr_core.uncertainty
    import hvsr_web.ondemand

    for module in (hvsr_core.core, hvsr_core.analysis, hvsr_web.ondemand):
        monkeypatch.setattr(module, "run_core", forbidden)
    for name in ("selected_mean_ci", "replicate_counts", "peak_replicates"):
        monkeypatch.setattr(hvsr_core.uncertainty, name, forbidden)
    monkeypatch.setattr(np.fft, "rfft", forbidden)
    runs_before = len(get_json(site, run["manifest_url"])["provenance"]["analysis_runs"])

    r = site.client.post(f"/api/v1/runs/{run_id}/plots", json=body, headers=site.auth)
    assert r.status_code == 201, r.text
    variant = r.json()
    again = site.client.post(f"/api/v1/runs/{run_id}/plots", json=body, headers=site.auth)
    assert again.status_code == 200 and again.json()["artifact_id"] == variant["artifact_id"]
    recipe = variant["generated_by"]["recipe"]
    assert (
        variant["generated_by"]["cosmetic_rerender"] is True
        and variant["artifact_id"] != published["artifact_id"]
    )
    analysis = one(per_method["sdof"], kind="analysis_json")
    ws = one(run["run_artifacts"], kind="window_spectra")
    assert variant["source_refs"] == [analysis["artifact_id"], ws["artifact_id"]]
    assert recipe["source_analysis_sha256"] == analysis["sha256"]
    assert recipe["series_sha256"] == published["generated_by"]["recipe"]["series_sha256"]
    assert recipe["individual_windows_rendered"] and recipe["show_alternate_method"]
    svg = download(site, variant["url"])[0].decode()
    doc = json.loads(download(site, analysis["url"])[0])
    sel = plots.main_selection(doc)
    assert sorted(re.findall(r'<g id="window-([^"]+)"', svg)) == sorted(sel["window_ids"])
    assert len(get_json(site, run["manifest_url"])["provenance"]["analysis_runs"]) == runs_before
    rb = runs.load_run(site.ctx, run_id)
    assert rb.by_method("sdof")["plot_svg"].id == published["artifact_id"], "published figure unchanged"
    assert DisplayOptions.model_validate(body["display"]).show_individual_windows

    # control: the same guards do stop a real scientific computation
    from hvsr_web import worker

    r = site.client.post(
        f"/api/v1/measurements/{sub['measurement_id']}/analyses",
        json={"robustness_check": False, "config": {"uncertainty": {"seed": 99}}},
        headers=site.auth,
    )
    assert r.status_code == 202, r.text
    worker.drain(site.ctx)
    assert get_json(site, r.json()["job_url"])["state"] != "succeeded"
    from hvsr_web.db import Job

    with site.ctx.sessions() as session:
        assert "a cosmetic change ran scientific code" in session.get(Job, r.json()["job_id"]).last_error


@pytest.mark.acceptance("AC-S7-15")
def test_figure_size_and_custom_variants_are_bounded(tmp_path):
    from stage8_support import CSV_GZ, analyze, make_site

    site = make_site(tmp_path, max_plot_pixels=2_000_000, max_plot_variants_per_run=2)
    sub = submit(site, [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")], "bounded-0001")
    huge = {"robustness_check": False, "display": {"figure_width_in": 30, "figure_height_in": 30, "dpi": 600}}
    r = site.client.post(
        f"/api/v1/measurements/{sub['measurement_id']}/analyses", json=huge, headers=site.auth
    )
    assert r.status_code == 422 and r.json()["detail"]["error"] == "compute_budget"
    assert r.json()["detail"]["problems"] == [
        "324000000 pixels in the PNG figure exceed the limit of 2000000"
    ]
    run_id = analyze(site, sub["measurement_id"], {"robustness_check": False})  # 1350 x 900 pixels

    def plot(fmt="svg", **display):
        body = {"method": "sdof", "format": fmt, "display": display}
        return site.client.post(f"/api/v1/runs/{run_id}/plots", json=body, headers=site.auth)

    r = plot("png", dpi=600)  # 5400 x 3600
    assert r.status_code == 422 and r.json()["detail"]["error"] == "plot_pixels"
    assert (
        plot("svg", dpi=600).status_code == 201
    )  # vector output: no raster to bound, still a custom variant
    assert plot("png", dpi=100).status_code == 201
    r = plot("png", dpi=120)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "plot_variant_limit"
    assert plot("png", dpi=100).status_code == 200  # an existing variant is served, not counted again
    assert plot("svg", show_alternate_method=True).status_code == 201  # default size: finite, uncapped
    assert site.client.get(f"/runs/{run_id}?method=sdof&alt=true").status_code == 200
    manifest = get_json(site, sub["manifest_url"])
    variants = [e for e in entries(manifest) if (e["generated_by"] or {}).get("cosmetic_rerender")]
    assert (
        len(variants) == 4
    )  # 2 custom-size + the API overlay + the page's compact overlay (desktop is shared)


@pytest.mark.acceptance("AC-S8-10")
def test_real_exports_validate_against_published_schemas(site, short_run, long_run):
    listed = get_json(site, "/api/v1/schemas")["schemas"]
    assert set(listed) == set(SCHEMA_FILES)
    for name, url in listed.items():
        r = site.client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/schema+json")
        assert r.json() == load_schema(name)
    assert validation_errors(get_json(site, "/api/v1/data-dictionary"), "data_dictionary") == []
    checked: dict[str, int] = {}

    def check(doc, name):
        assert validation_errors(doc, name) == [], name
        checked[name] = checked.get(name, 0) + 1

    for pub in (short_run, long_run):
        manifest = get_json(site, f"/api/v1/measurements/{pub.measurement_id}/manifest")
        check(manifest, "manifest")
        for e in entries(manifest):
            if e["availability"] != "available":
                continue
            if e["kind"] == "analysis_json":
                check(json.loads(download(site, e["url"])[0]), "analysis")
            elif e["kind"] == "window_table_json":
                check(json.loads(download(site, e["url"])[0]), "window_table")
            elif e["kind"] == "template_bank_manifest":
                check(json.loads(download(site, e["url"])[0]), "template_bank")
            elif e["kind"].startswith("plot_"):
                check(e["generated_by"]["recipe"], "plot_recipe")
        for r in manifest["provenance"]["analysis_runs"]:
            check(get_json(site, r["run_url"])["request"], "config")
    assert checked["analysis"] >= 6 and checked["window_table"] >= 6 and checked["plot_recipe"] >= 18
    assert checked["template_bank"] >= 1 and checked["manifest"] == 2 and checked["config"] == 3


def _published_csvs(site, pub):
    manifest = get_json(site, f"/api/v1/measurements/{pub.measurement_id}/manifest")
    for e in entries(manifest):
        if e["kind"] == "mean_spectrum_csv":
            yield e, "mean_spectrum"
        elif e["kind"] == "window_table_csv":
            yield e, e["generated_by"]["table"]


@pytest.mark.acceptance("AC-S8-13")
def test_numeric_csvs_follow_the_dictionary_contract(site, short_run, long_run):
    seen = set()
    for pub in (short_run, long_run):
        for e, table in _published_csvs(site, pub):
            data, resp = download(site, e["url"])
            assert resp.headers["content-type"].startswith("text/csv")
            assert exports.validate_csv(data, table) == [], (
                e["label"],
                exports.validate_csv(data, table)[:5],
            )
            header = data.split(b"\n", 1)[0].decode()
            assert header.split(",") == [c["name"] for c in exports.TABLES[table]["columns"]]
            assert not re.search(r"(^|,)(nan|NaN|inf|Infinity|null|None)(,|$)", data.decode(), flags=re.M)
            seen.add(table)
    assert seen == {"mean_spectrum", "window_table_standard", "window_table_sdof"}
    # units are explicit: physical units in the header name, every unit in the served dictionary
    dictionary = get_json(site, "/api/v1/data-dictionary")
    assert dictionary["csv_conventions"]["encoding"].startswith("UTF-8")
    for name, table in dictionary["tables"].items():
        for col in table["columns"]:
            assert "unit" in col, (name, col["name"])
            if col["unit"] == "Hz":
                assert col["name"].endswith("_hz"), col["name"]
            if col["unit"] == "s":
                assert col["name"].endswith("_s"), col["name"]


@pytest.mark.acceptance("AC-S8-13")
def test_unavailable_numbers_are_blank_with_a_reason_never_zero(site, short_run):
    run, per_method = _run_items(site, short_run.run_id)
    blanks = 0
    for items in per_method.values():
        doc, _ = _doc(site, items)
        rows = csv_rows(download(site, one(items, kind="mean_spectrum_csv")["url"])[0])
        by_key = {(r["selection_id"], r["frequency_hz"]): r for r in rows}
        why = {
            "hv_geometric_mean": "all values",
            "ci95_lower": "ci95",
            "ci95_upper": "ci95",
            "log_std": "log_std",
        }
        for sel in doc["selections"]:
            unc, mc = sel["uncertainty"], sel["mean_curve"]
            for i, f in enumerate(doc["grid"]["frequency_hz"]):
                row = by_key[(sel["selection_id"], repr(float(f)))]
                for k in why:
                    if mc[k][i] is None:
                        assert row[k] == "", (k, row)
                        assert why[k] in row["reason"] or "all values" in row["reason"], row
                        blanks += 1
                    else:
                        assert float(row[k]) == mc[k][i]
                if (
                    doc["grid"]["valid"][i]
                    and mc["hv_geometric_mean"][i] is not None
                    and mc["ci95_lower"][i] is None
                ):
                    assert row["status"] == "ci_unavailable"
                    if unc["status"] == "ci_not_estimable":
                        assert unc["status_reason"] in row["reason"]
    assert blanks > 0, "the 600 s run must exercise unavailable values (CI not estimable)"
    # per-window tables: blank cells always explained, never zero placeholders for missing values
    for items in per_method.values():
        rows = csv_rows(download(site, one(items, kind="window_table_csv")["url"])[0])
        rejected = [r for r in rows if r["outcome"] != "selected"]
        assert rejected, "rejected candidates are listed too"
        for r in rows:
            empty = [k for k, v in r.items() if v == "" and k != "reason"]
            if empty:
                assert r["reason"], r
            assert r["selection_order"] != "0"


@pytest.mark.acceptance("AC-S8-13")
@pytest.mark.acceptance("AC-S8-04")
def test_on_request_csvs_validate(fresh):
    site = fresh
    sub = submit(site, [(MSEED, fixture_bytes(MSEED), "waveform")], "csv-mseed-0001", lat=-33.45, lon=-70.66)
    run_id = analyze_quick(site, sub["measurement_id"])
    manifest = get_json(site, sub["manifest_url"])
    ws = one(entries(manifest), kind="window_spectra", run_id=run_id)
    data, _ = download(site, ws["url"])
    assert exports.validate_csv(gzip.decompress(data), "window_spectra") == []
    norm = one(entries(manifest), kind="normalized_csv")
    data, resp = download(site, norm["url"])
    assert resp.headers["content-type"].startswith("application/gzip")
    assert exports.validate_csv(gzip.decompress(data), "normalized_waveform") == []


def analyze_quick(site, measurement_id):
    from stage8_support import analyze

    return analyze(site, measurement_id, {"robustness_check": False})
