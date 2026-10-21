"""Separate downloads (AC-S8-03), normalized waveform CSV (AC-S8-04), missing originals (AC-S8-05),
and manifest links (AC-S8-09)."""

from __future__ import annotations

import gzip
import io
import json
import re

import numpy as np
import obspy
import pytest

from hvsr_core import exports
from hvsr_core.jsonutil import sha256_bytes
from hvsr_web import worker

from stage8_support import (
    CSV_GZ,
    MSEED,
    analyze,
    csv_rows,
    download,
    element_text,
    entries,
    fixture_bytes,
    get_json,
    one,
    reopen,
    submit,
)


@pytest.mark.acceptance("AC-S8-03")
def test_original_canonical_numerical_and_plot_downloads_are_separate(site, short_run):
    manifest = get_json(site, f"/api/v1/measurements/{short_run.measurement_id}/manifest")
    cats = manifest["categories"]
    all_entries = entries(manifest)
    urls = [e["url"] for e in all_entries]
    assert len(urls) == len(set(urls)), "one stable endpoint per artifact"
    assert all(
        re.fullmatch(r"/api/v1/artifacts/art_[0-9a-f]{24}|/api/v1/artifacts/[0-9a-f-]{32,36}", u)
        for u in urls
    )

    original = one(cats["original_data"], kind="original_csv_gz")
    data, resp = download(site, original["url"])
    assert data == fixture_bytes(CSV_GZ) and sha256_bytes(data) == original["sha256"]
    assert original["label"].startswith("Original") and resp.headers["content-type"] == "application/gzip"
    assert f'filename="{CSV_GZ}"' in resp.headers["content-disposition"]

    canonical = {e["kind"]: e for e in cats["canonical_recordings"]}
    assert {"canonical_miniseed", "normalized_csv"} <= set(canonical)
    assert not any(e["label"].startswith("Original") for e in cats["canonical_recordings"])
    assert canonical["canonical_miniseed"]["url"] != original["url"]

    expected = {
        "analysis_runs": ("application/json", {"analysis_json", "window_table_json"}),
        "spectra": ("text/csv", {"mean_spectrum_csv"}),
        "plots": (None, {"plot_png", "plot_svg", "plot_svg_compact"}),
    }
    for category, (mime, kinds) in expected.items():
        run_items = [e for e in cats[category] if e["run_id"] == short_run.run_id]
        assert kinds <= {e["kind"] for e in run_items}, category
        for method in ("standard", "sdof"):
            for kind in kinds:
                e = one(run_items, kind=kind, method=method)
                body, resp = download(site, e["url"])
                assert sha256_bytes(body) == e["sha256"] and len(body) == e["byte_size"]
                assert resp.headers["content-type"].split(";")[0] == (mime or e["mime_type"])
    png = one(cats["plots"], kind="plot_png", method="sdof", run_id=short_run.run_id)
    assert download(site, png["url"])[0][:8] == b"\x89PNG\r\n\x1a\n"
    photo = one(cats["sensor_metadata"], kind="photo_sensor")
    assert download(site, photo["url"])[1].headers["content-type"].startswith("image/")
    # pages link each product individually (no bundle replaces an individual endpoint)
    page = site.client.get(f"/measurements/{short_run.measurement_id}").text
    for e in cats["original_data"] + cats["canonical_recordings"] + cats["sensor_metadata"]:
        assert f'href="{e["url"]}"' in page, e["kind"]
    run_page = site.client.get(f"/runs/{short_run.run_id}?method=sdof").text
    for e in all_entries:
        if e["run_id"] == short_run.run_id and e["method"] in ("sdof", None):
            assert f'href="{e["url"]}"' in run_page, e["kind"]


@pytest.mark.acceptance("AC-S8-04")
def test_normalized_waveform_csv_from_miniseed_matches_an_independent_decode(fresh):
    site = fresh
    sub = submit(site, [(MSEED, fixture_bytes(MSEED), "waveform")], "normalized-0001", lat=-33.45, lon=-70.66)
    manifest = get_json(site, sub["manifest_url"])
    norm = one(entries(manifest), kind="normalized_csv")
    assert norm["availability"] == "on_demand" and norm["availability_reason"]
    first = site.client.get(norm["url"])
    assert first.status_code == 202 and first.headers["retry-after"] == "5"
    assert get_json(site, first.json()["job_url"])["state"] == "queued"
    worker.drain(site.ctx)
    data, resp = download(site, norm["url"])
    assert resp.headers["content-type"] == "application/gzip"
    text = gzip.decompress(data)
    assert exports.validate_csv(text, "normalized_waveform") == []
    rows = csv_rows(text)
    header = list(rows[0])
    assert header == [
        "sample_index",
        "time_offset_s",
        "time_utc",
        "N_unknown_units",
        "E_unknown_units",
        "Z_unknown_units",
    ]

    st = obspy.read(io.BytesIO(fixture_bytes(MSEED)))  # independent decoder: ObsPy
    expected = {tr.stats.channel[-1]: tr.data for tr in st}
    assert len(rows) == len(expected["Z"])
    for comp in "NEZ":
        got = np.array([int(r[f"{comp}_unknown_units"]) for r in rows], dtype=np.int64)
        assert np.array_equal(got, expected[comp].astype(np.int64)), comp
    t0 = st[0].stats.starttime
    assert rows[0]["time_utc"].startswith(t0.strftime("%Y-%m-%dT%H:%M:%S"))
    assert rows[1]["time_offset_s"] == repr(1 / 50) and rows[-1]["sample_index"] == str(len(rows) - 1)
    # the artifact is now stored: later requests redirect straight to it, and it is listed as available
    again = get_json(site, sub["manifest_url"])
    stored = one(entries(again), kind="normalized_csv")
    assert stored["availability"] == "available" and stored["sha256"] == sha256_bytes(data)
    assert site.client.get(norm["url"], follow_redirects=False).status_code == 307


@pytest.mark.acceptance("AC-S8-05")
def test_missing_originals_are_disclosed_with_an_explicit_state(fresh):
    site = fresh
    sub = submit(
        site,
        [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")],
        "canonical-only-0001",
        waveform={"csv_layout": "elapsed_time", "canonical_only": True},
    )
    manifest = get_json(site, sub["manifest_url"])
    assert manifest["original_bytes_retained"] is False and manifest["original_bytes_note"]
    orig = one(manifest["categories"]["original_data"], kind="original_csv_gz")
    assert orig["availability"] == "unavailable" and orig["availability_reason"]
    assert orig["sha256"] == sha256_bytes(fixture_bytes(CSV_GZ)), "the discarded file's checksum is kept"
    r = site.client.get(orig["url"])
    assert r.status_code == 404
    assert r.json() == {
        "error": "unavailable",
        "detail": orig["availability_reason"],
        "sha256": orig["sha256"],
    }

    available = [e for e in entries(manifest) if e["availability"] in ("available", "on_demand")]
    assert not any(re.match(r"Original CSV", e["label"]) for e in available)
    labels = {e["kind"]: e["label"] for e in manifest["categories"]["canonical_recordings"]}
    assert labels["canonical_miniseed"] == "Canonical recording"
    assert labels["reconstructed_csv"] == "Reconstructed CSV"

    page = site.client.get(f"/measurements/{sub['measurement_id']}").text
    notice = element_text(page, "original-missing")
    assert manifest["original_bytes_note"] in notice and orig["availability_reason"] in notice
    row = re.search(r'<tr data-availability="unavailable">.*?</tr>', page, re.S).group(0)
    assert orig["url"] not in row, "an unavailable artifact is listed but not linked"
    assert "Unavailable" in row or "unavailable" in row
    recon = one(entries(manifest), kind="reconstructed_csv")
    data, _ = download(site, recon["url"])
    disclosure, header = data.decode("utf-8").splitlines()[:2]
    assert disclosure.startswith("# Reconstructed CSV") and "not retained" in disclosure
    assert header == "time_s,N,E,Z"
    assert sha256_bytes(data) != orig["sha256"], "a reconstruction is not presented as the original bytes"


@pytest.mark.acceptance("AC-S8-05")
def test_retained_originals_page_says_so(site, short_run):
    page = site.client.get(f"/measurements/{short_run.measurement_id}").text
    assert 'id="original-missing"' not in page
    manifest = get_json(site, f"/api/v1/measurements/{short_run.measurement_id}/manifest")
    assert manifest["original_bytes_retained"] is True
    assert all(e["availability"] == "available" for e in manifest["categories"]["original_data"])


def _links(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and (k == "url" or k.endswith("_url")):
                yield f"{path}.{k}", v
            else:
                yield from _links(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _links(v, f"{path}[{i}]")


# Absolute links are citations of external documents (not fetched offline); anything else must be ours.
EXTERNAL_REFERENCES = {
    "https://creativecommons.org/licenses/by/4.0/",
    "http://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf",
}


def _check_link(site, where, url, entry=None):
    if re.match(r"https?://", url):
        assert url in EXTERNAL_REFERENCES, (where, url)
        return "external"
    assert url.startswith("/") and "/blobs/" not in url and "sig=" not in url, (where, url)
    r = site.client.get(url)
    if entry is None:
        assert r.status_code == 200, (where, url, r.status_code)
        return "resolves"
    state = entry["availability"]
    if state == "available":
        assert r.status_code == 200 and sha256_bytes(r.content) == entry["sha256"], (where, url)
        assert len(r.content) == entry["byte_size"]
    elif state == "on_demand":
        assert r.status_code == 202 and entry["availability_reason"], (where, url)
        assert site.client.get(r.json()["job_url"]).status_code == 200
    else:
        assert state in ("unavailable", "removed") and entry["availability_reason"], (where, entry)
        assert r.status_code in (404, 410) and r.json()["error"] in ("unavailable", "removed")
    return state


@pytest.mark.acceptance("AC-S8-09")
def test_every_manifest_link_resolves_or_has_an_explicit_state(site, short_run, long_run):
    seen: dict[str, int] = {}
    in_documents = 0
    for pub in (short_run, long_run):
        manifest = get_json(site, f"/api/v1/measurements/{pub.measurement_id}/manifest")
        by_url = {e["url"]: e for e in entries(manifest)}
        ids = {e["artifact_id"] for e in entries(manifest)}
        for e in entries(manifest):
            assert set(e["source_refs"]) <= ids, (e["kind"], e["source_refs"])
        for where, url in _links(manifest):
            state = _check_link(site, where, url, by_url.get(url))
            seen[state] = seen.get(state, 0) + 1
        # links inside the published documents and the run summaries resolve as well
        for r in manifest["provenance"]["analysis_runs"]:
            run = get_json(site, r["run_url"])
            for where, url in _links(run):
                if url.startswith("/runs/"):
                    assert site.client.get(url).status_code == 200
                    continue
                _check_link(site, where, url, by_url.get(url))
        for e in entries(manifest):
            if e["kind"] == "analysis_json":
                doc = json.loads(download(site, e["url"])[0])
                for where, url in _links(doc):
                    if _check_link(site, f"{e['label']}{where}", url, by_url.get(url)) != "external":
                        in_documents += 1
    assert in_documents >= 6, "candidates_url / comparison links inside the analysis documents"
    assert seen.get("available", 0) > 40 and seen.get("on_demand", 0) >= 4
    assert seen.get("resolves", 0) >= 4 and seen.get("external") == 2


@pytest.mark.acceptance("AC-S8-09")
def test_failed_generation_is_an_explicit_state_not_a_broken_link(fresh):
    site = fresh
    sub = submit(site, [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")], "generation-fail-01")
    run_id = analyze(site, sub["measurement_id"], {"robustness_check": False})
    ws = one(entries(get_json(site, sub["manifest_url"])), kind="window_spectra", run_id=run_id)
    assert ws["availability"] == "on_demand"
    tight = reopen(site, max_window_spectra_cells=10)  # the operator lowered the limit after publication
    assert tight.client.get(ws["url"]).status_code == 202
    worker.drain(tight.ctx)
    r = tight.client.get(ws["url"])
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "generation_failed" and "limit" in body["detail"]
    assert get_json(tight, body["job_url"])["state"] == "dead"
    entry = one(entries(get_json(tight, sub["manifest_url"])), kind="window_spectra")
    assert entry["availability"] == "on_demand" and entry["availability_reason"]
