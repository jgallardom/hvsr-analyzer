"""License and attribution on public pages (AC-S8-08); centralized English strings (AC-S8-12)."""

from __future__ import annotations

import json
import re
from typing import get_args

import pytest
from matplotlib import rc_context
from matplotlib.text import Text

from hvsr_core import plots, strings
from hvsr_core.config import Spectral
from hvsr_core.io.csv_reader import Layout
from hvsr_core.metadata import Context, Level, RecordedQuantity
from hvsr_core.schema_validation import load_schema
from hvsr_web import pages, runs
from hvsr_web.manifests import CATEGORIES
from hvsr_web.submissions import Role

from stage8_support import (
    CSV_GZ,
    ROOT,
    admin_token,
    analyze,
    download,
    element_text,
    entries,
    fixture_bytes,
    get_json,
    one,
    page_text,
    png_photo,
    submit,
)

CC_BY = '<a href="https://creativecommons.org/licenses/by/4.0/" rel="license">'
TEMPLATES = ROOT / "src/hvsr_web/templates"
APP_JS = ROOT / "src/hvsr_web/static/app.js"
SOURCES = [*(ROOT / "src/hvsr_web").glob("*.py"), *(ROOT / "packages/hvsr_core/src/hvsr_core").glob("*.py")]


# --------------------------------------------------------------------------------------- AC-S8-08
def _footer_license(html: str) -> None:
    footer = element_text(html, "data-license")
    assert "Creative Commons Attribution 4.0" in footer and "attribution" in footer
    assert CC_BY in html and "Software license: BSD-3-Clause." in html


@pytest.mark.acceptance("AC-S8-08")
def test_license_and_attribution_on_every_public_page(site, short_run, long_run):
    c = site.client
    mid, code = short_run.measurement_id, short_run.record_code
    urls = [
        "/",
        "/search?code=" + code[:5],
        "/upload",
        "/data",
        f"/measurements/{mid}",
        f"/measurements/{mid}/configure",
        f"/runs/{short_run.run_id}",
        f"/runs/{short_run.run_b_id}?method=sdof",
        f"/runs/{long_run.run_id}?method=sdof",
        "/runs/r-unknown",
    ]
    for url in urls:
        r = c.get(url)
        assert r.status_code == (404 if url.endswith("unknown") else 200), url
        _footer_license(r.text)

    manifest = get_json(site, f"/api/v1/measurements/{mid}/manifest")
    lic = manifest["licensing"]
    assert lic["license_id"] == "CC-BY-4.0" and lic["attribution"] == "Example Observatory"
    page = c.get(f"/measurements/{mid}").text
    section = element_text(page, "license")
    assert "Example Observatory" in section and "CC-BY-4.0" in section
    assert lic["suggested_citation"] in section
    assert re.fullmatch(
        rf"Example Observatory \(20\d\d\)\. HVSR measurement {re.escape(code)}\. HVSR Analyzer, "
        rf"/measurements/{mid}\. CC BY 4\.0\.",
        lic["suggested_citation"],
    )
    assert f'<a href="{lic["license_url"]}" rel="license" translate="no">CC-BY-4.0</a>' in page
    for run_id in (short_run.run_id, short_run.run_b_id):
        run_page = c.get(f"/runs/{run_id}").text
        attribution = element_text(run_page, "attribution")
        assert "Example Observatory" in attribution and "CC-BY-4.0" in attribution
        assert f'href="/measurements/{mid}#license"' in run_page
    analyses = [e for e in entries(manifest) if e["kind"] == "analysis_json"]
    assert len(analyses) == 4
    for e in analyses:
        embedded = json.loads(download(site, e["url"])[0])["licensing"]
        assert {k: embedded[k] for k in ("license_id", "license_url", "attribution", "source_citation")} == {
            k: lic[k] for k in ("license_id", "license_url", "attribution", "source_citation")
        }
        assert (
            embedded["record_code"] == code
            and embedded["manifest_url"] == f"/api/v1/measurements/{mid}/manifest"
        )
    records = c.get("/").text
    assert f"{code}</a>" in records and "Example Observatory" in records
    upload = c.get("/upload").text
    consent = get_json(site, "/api/v1/limits")["consent"]
    assert (
        consent["text"] in upload.replace("&#39;", "'").replace("&#34;", '"')
        and "CC BY 4.0" in consent["text"]
    )


@pytest.mark.acceptance("AC-S8-08")
def test_removed_measurement_page_keeps_license_and_explains_removal(fresh):
    site = fresh
    sub = submit(site, [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")], "removal-page-0001")
    admin = admin_token(site)
    r = site.client.post(
        f"/api/v1/admin/measurements/{sub['measurement_id']}/remove",
        data={"reason": "contributor asked for removal"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 200, r.text
    page = site.client.get(f"/measurements/{sub['measurement_id']}")
    assert page.status_code == 410 and "removed" in page.text.lower()
    _footer_license(page.text)
    manifest = get_json(site, sub["manifest_url"])
    assert manifest["licensing"]["attribution"] == "Example Observatory"
    assert manifest["licensing"]["removal_notice"]


# --------------------------------------------------------------------------------------- AC-S8-12
CATALOG = strings.catalog("en")


def _enum(schema: str, *path: str) -> list[str]:
    node = load_schema(schema)
    for p in path:
        node = node[p]
    return node["enum"]


# Every dynamic key prefix used in code or templates, with the authoritative list of its values.
DYNAMIC: dict[str, list[str]] = {
    "availability.": _enum("manifest", "$defs", "artifact", "properties", "availability"),
    "calibration.": _enum("analysis", "properties", "calibration", "properties", "status"),
    "category.": list(CATEGORIES),
    "context.": list(get_args(Context)),
    "instrument.": _enum("analysis", "properties", "instrument", "properties", "status"),
    "layout.": list(get_args(Layout)),
    "level.": list(get_args(Level)),
    "method.": _enum("analysis", "properties", "method"),
    "plot.mean.": _enum("analysis", "properties", "method"),
    "quantity.": list(get_args(RecordedQuantity)),
    "role.": ["primary", "robustness_b"],
    "status.": _enum("manifest", "properties", "moderation", "properties", "status"),
    "combination.": list(get_args(Spectral.model_fields["horizontal_combination"].annotation)),
    "artifact.": [*runs.PLOT_KIND.values(), *(r for r in get_args(Role) if r.startswith("photo_"))],
    "artifact.original_": ["csv", "csv.gz", "miniseed", "miniseed3"],
}
LITERAL_KEY = re.compile(r"""\bt\(\s*["']([a-z0-9_]+(?:\.[a-z0-9_.]+)?)["']\s*[,)]""")
DYNAMIC_KEY = re.compile(r"""\bt\(\s*f?["']([a-z0-9_.]+?)(?:["']\s*~|\{)""")
VERBATIM_KEY = re.compile(r"""verbatim\([^)]*,\s*["']([a-z0-9_.]+)["']\)""")


@pytest.mark.acceptance("AC-S8-12")
def test_every_key_used_exists_and_dynamic_keys_are_enumerated():
    texts = {p: p.read_text() for p in [*TEMPLATES.glob("*.html"), *SOURCES]}
    used, prefixes = set(), set()
    for p, text in texts.items():
        used |= set(LITERAL_KEY.findall(text)) | set(VERBATIM_KEY.findall(text))
        prefixes |= set(DYNAMIC_KEY.findall(text))
    assert len(used) > 250
    assert sorted(k for k in used if k not in CATALOG) == []
    assert prefixes and sorted(prefixes - set(DYNAMIC)) == [], "a new dynamic prefix needs its value list"
    missing = [p + v for p, values in DYNAMIC.items() for v in values if p + v not in CATALOG]
    assert missing == []
    assert all(k in CATALOG for k in pages.JS_KEYS)
    js_keys = set(re.findall(r"""\bS\(\s*["']([a-z0-9_.]+)["']""", APP_JS.read_text()))
    assert js_keys and sorted(js_keys - set(pages.JS_KEYS)) == []
    # placeholders are consistent: a catalog value never contains a stray unformatted brace
    for k, v in CATALOG.items():
        assert re.fullmatch(r"([^{}]|\{[a-z_0-9]+\})*", v), k


def _without_catalog_text(text: str) -> str:
    while True:
        stripped = re.sub(r"‹[^‹›]*›", "", text)
        if stripped == text:
            return text
        text = stripped


def _interface_problems(html: str) -> list[str]:
    """Interface text (outside translate="no") that does not come from the catalog."""
    return [
        text.strip()[:80]
        for text in page_text(html).interface
        if re.search(r"[A-Za-z]{2,}", _without_catalog_text(text))
    ]


@pytest.mark.acceptance("AC-S8-12")
def test_templates_contain_no_literal_interface_text():
    for path in sorted(TEMPLATES.glob("*.html")):
        src = path.read_text()
        src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
        src = re.sub(r"\{%.*?%\}", "", src, flags=re.S)
        src = re.sub(r"\{\{.*?\}\}", "", src, flags=re.S)
        assert _interface_problems(src) == [], path.name


@pytest.mark.acceptance("AC-S8-12")
def test_javascript_has_no_literal_interface_text():
    js = re.sub(r"^\s*//.*$", "", APP_JS.read_text(), flags=re.M)
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', js)
    values = [next(v for v in group if v) for group in literals if any(group)]
    assert len(values) > 50
    phrases = [v for v in values if re.search(r"[A-Za-z]{2,}[,.;:!?]*\s+[A-Za-z]{2,}", v)]
    assert phrases == ["use strict"], phrases
    assert not re.search(r"textContent\s*=\s*([\"'`])[^\"'`]*[A-Za-z]", js)
    assert "innerHTML" not in js


@pytest.mark.acceptance("AC-S8-12")
def test_the_interface_text_check_is_sensitive(site, short_run):
    assert _interface_problems("<p>Upload a recording</p>") == ["Upload a recording"]
    assert (
        _interface_problems('<p>‹Upload a recording› <span translate="no">F5 peak_found</span> 2.5</p>') == []
    )
    assert _interface_problems('<img alt="Sensor photo" src="x">') == ["Sensor photo"]
    real = site.client.get(f"/runs/{short_run.run_id}").text  # the English catalog is not marked
    assert len(_interface_problems(real)) > 50


@pytest.fixture
def pseudo_locale(monkeypatch):
    fake = {k: f"‹{v}›" for k, v in CATALOG.items()}
    monkeypatch.setattr(strings, "catalog", lambda lang="en": fake)
    return fake


@pytest.mark.acceptance("AC-S8-12")
def test_pages_render_every_interface_string_from_the_catalog(fresh, pseudo_locale):
    """Pseudo-localization: every catalog value is wrapped in ‹ ›, so any interface text that still reads
    as plain words outside translate="no" content was not centralized."""
    site, c = fresh, fresh.client
    files = [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform"), ("area.png", png_photo(), "photo_area")]
    sub = submit(
        site, files, "pseudo-0001", metadata={"ground_surface": "gravel", "observations": "light rain"}
    )
    run_id = analyze(site, sub["measurement_id"])
    co = submit(
        site,
        [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")],
        "pseudo-0002",
        waveform={"csv_layout": "elapsed_time", "canonical_only": True},
    )
    removed = submit(site, [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform")], "pseudo-0003", lat=1.0, lon=1.0)
    c.post(
        f"/api/v1/admin/measurements/{removed['measurement_id']}/remove",
        data={"reason": "pseudo-locale removal check"},
        headers={"Authorization": f"Bearer {admin_token(site)}"},
    )
    run = get_json(site, f"/api/v1/runs/{run_id}")
    doc = json.loads(download(site, run["methods"]["sdof"]["analysis_url"])[0])
    families = [s["selection_id"] for s in doc["selections"]]
    ws = one(run["run_artifacts"], kind="window_spectra")
    mid = sub["measurement_id"]
    urls = [
        "/",
        "/search?code=zz",
        "/upload",
        "/data",
        f"/measurements/{mid}",
        f"/measurements/{mid}/configure",
        f"/measurements/{co['measurement_id']}",
        f"/measurements/{removed['measurement_id']}",
        f"/runs/{run_id}",
        f"/runs/{run_id}?method=sdof&alt=true",
        f"/runs/{run_id}?method=sdof&windows=true",
        f"/runs/{run_id}?method=sdof&sel={families[-1]}",
        f"/runs/{get_json(site, sub['manifest_url'])['provenance']['analysis_runs'][-1]['run_id']}",
        "/runs/nope",
    ]
    rendered = 0
    for url in urls:
        r = c.get(url)
        assert r.status_code in (200, 404, 410), url
        assert _interface_problems(r.text) == [], url
        assert "‹" in r.text
        rendered += 1
    download(site, ws["url"])  # per-window spectra now stored: the windows variant renders for real
    r = c.get(f"/runs/{run_id}?method=sdof&windows=true")
    assert (
        _interface_problems(r.text) == [] and 'class="notice"' not in r.text.split('id="ci-note"')[0][-400:]
    )
    assert rendered == len(urls)


@pytest.mark.acceptance("AC-S8-12")
def test_figure_text_comes_from_the_catalog(site, long_run, pseudo_locale):
    run = get_json(site, f"/api/v1/runs/{long_run.run_id}")
    for method in run["methods"]:
        doc = json.loads(download(site, run["methods"][method]["analysis_url"])[0])
        recipe = plots.make_recipe(doc, fmt="svg", layout="compact")
        assert recipe["title"].startswith("‹") and recipe["title"].endswith("›")
        footer = " ".join(recipe["footer"])
        assert not re.search(r"[A-Za-z]{2,}", _without_catalog_text(footer)), footer
        with rc_context(plots.RC):
            fig, _ = plots.build_figure(doc, recipe)
        ax = fig.axes[0]
        for label in (ax.get_xlabel(), ax.get_ylabel()):
            assert label.startswith("‹"), label
        legend = [t.get_text() for t in fig.legends[0].get_texts()]
        for text in legend:
            # the band label is the specified scientific label carried by the analysis document
            assert text == doc["ci_label"] or text.startswith("‹"), text
        assert any(isinstance(t, Text) for t in fig.findobj(Text))
