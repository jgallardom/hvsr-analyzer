"""Figures: provenance footer without SESAME (AC-S8-06), readability on desktop and mobile (AC-S8-07),
title and the single labeled band (AC-S8-11)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import PolyCollection
from matplotlib.text import Text

from hvsr_core import plots
from hvsr_web import pages

from stage8_support import BAND_LABEL, ROOT, download, element_text, get_json, one, svg_texts

CSS = (ROOT / "src/hvsr_web/static/style.css").read_text()
MIN_CSS_PX = 9.0  # smallest acceptable rendered figure text, in CSS pixels
MIN_VIEWPORT_PX = 340  # narrowest supported phone viewport (CSS pixels)
SCROLLBAR_PX = 17  # classic desktop scrollbar; phones use overlay scrollbars (0)


def _run(site, run_id):
    run = get_json(site, f"/api/v1/runs/{run_id}")
    return run, {m: v["artifacts"] for m, v in run["methods"].items()}


def _doc(site, items):
    return json.loads(download(site, one(items, kind="analysis_json")["url"])[0])


def _figure(doc, recipe, alt_doc=None):
    with rc_context(plots.RC):
        fig, _ = plots.build_figure(doc, recipe, alternate_doc=alt_doc)
    FigureCanvasAgg(fig).draw()
    return fig


# --------------------------------------------------------------------------------------- AC-S8-06
@pytest.mark.acceptance("AC-S8-06")
@pytest.mark.parametrize("which", ["short", "long"])
def test_plot_footer_carries_the_required_provenance(site, short_run, long_run, which):
    pub = short_run if which == "short" else long_run
    _, per_method = _run(site, pub.run_id)
    for method, items in per_method.items():
        doc = _doc(site, items)
        for kind in ("plot_svg", "plot_svg_compact"):
            art = one(items, kind=kind)
            recipe = art["generated_by"]["recipe"]
            svg = download(site, art["url"])[0].decode()
            footer = " ".join(recipe["footer"])
            drawn = [s for s in svg_texts(svg) if s in recipe["footer"]]
            assert sorted(drawn) == sorted(recipe["footer"]), "every footer line is drawn once"
            sel = plots.main_selection(doc, recipe["selection_id"])
            cfg, rec, unc = doc["config"], doc["recording"], sel["uncertainty"]
            required = [
                doc["record"]["record_code"],
                f"({pub.lat}, {pub.lon})",
                doc["run_id"],
                {"standard": "Standard", "sdof": "SDOF-selected"}[method],
                f"peak family {sel['peak_family_id']}" if sel["peak_family_id"] else "peak family none",
                f"Lw {rec['window_length_s']:g} s",
                f"step {rec['step_s']:g} s",
                f"n = {sel['n_selected']}",
                f"b = {cfg['spectral']['ko_bandwidth']:g}",
                f"search {cfg['peak_search']['fmin_hz']:g}-{cfg['peak_search']['fmax_hz']:g} Hz",
                f"time {rec['analyzed_start_s']:g}-{rec['analyzed_end_s']:g} s",
                "RMS horizontal combination",
                f"detrend {cfg['preprocessing']['detrend']}",
                f"Tukey {cfg['preprocessing']['taper_alpha']:g}",
                "band-pass none",
                f"correction {cfg['preprocessing']['response_correction']}",
                "uncalibrated (provisional)",
                doc["provenance"]["software_version"],
                recipe["rendering_version"],
            ]
            bank = doc["provenance"].get("template_bank")
            if method == "sdof":
                assert bank, "the SDOF method records its template bank"
                required.append(f"templates {bank['bank_id']}")
            if recipe["band"]["drawn"]:
                required += [
                    BAND_LABEL,
                    unc["method"],
                    f"{unc['n_bootstrap']} replicates",
                    f"{unc['informative_blocks']} informative blocks",
                    "nominal, coverage not calibrated",
                ]
            else:
                required += ["Band: not drawn", unc["status"], unc["status_reason"]]
            missing = [r for r in required if r not in footer]
            assert not missing, (method, kind, missing, recipe["footer"])
            assert len(recipe["footer"]) <= (8 if kind == "plot_svg" else 14), "footer stays compact"


@pytest.mark.acceptance("AC-S8-06")
def test_figures_contain_no_sesame_explanations(site, short_run, long_run):
    checked = 0
    for pub in (short_run, long_run):
        manifest = get_json(site, f"/api/v1/measurements/{pub.measurement_id}/manifest")
        plots_ = manifest["categories"]["plots"]
        for art in plots_:
            image = download(site, art["url"])[0]
            assert b"sesame" not in image.lower(), art["label"]
            assert "sesame" not in json.dumps(art["generated_by"]["recipe"]).lower()
            checked += 1
        for e in manifest["categories"]["analysis_runs"]:
            if e["kind"] == "analysis_json" and e["availability"] == "available":
                doc = json.loads(download(site, e["url"])[0])
                assert doc["sesame_requested"], (
                    "SESAME was evaluated, so its absence from figures is a choice"
                )
                recipe = plots.make_recipe(doc, fmt="png")
                fig = _figure(doc, recipe)
                texts = [t.get_text() for t in fig.findobj(Text)]
                assert texts and not any("sesame" in t.lower() for t in texts)
    assert checked >= 18  # (2 + 1 runs) x 2 methods x 3 formats


# --------------------------------------------------------------------------------------- AC-S8-07
def _css_block(selector_pattern: str, css: str) -> str:
    m = re.search(selector_pattern + r"\s*\{([^}]*)\}", css)
    assert m, selector_pattern
    return m.group(1)


def _media(max_width: int) -> str:
    m = re.search(rf"@media \(max-width: {max_width}px\) \{{(.*?)\n\}}", CSS, re.S)
    assert m, max_width
    return m.group(1)


def _px(block: str, prop: str) -> float | None:
    m = re.search(rf"(?:^|[;\s]){prop}:\s*(-?\d+(?:\.\d+)?)px", block)
    return float(m.group(1)) if m else None


def rendered_width(viewport: int, layout: str, scrollbar: int) -> float:
    """CSS width of the figure's drawing area, derived from the stylesheet rules (checked below)."""
    wrap = _css_block(r"\.wrap", CSS)
    wrap_max, pad = _px(wrap, "max-width"), float(re.search(r"padding:\s*0\s+(\d+)px", wrap).group(1))
    base = _css_block(r"\.hv-figure img", CSS)
    border = float(re.search(r"border:\s*(\d+)px", base).group(1))
    layout_w = viewport - scrollbar
    column = min(layout_w, wrap_max) - 2 * pad
    if viewport > pages.FIGURE_BREAKPOINT_PX:
        assert layout == "desktop"
        return min(column, _px(base, "max-width")) - 2 * border
    assert layout == "compact"
    cap = _px(_css_block(r"\.hv-figure img", _media(pages.FIGURE_BREAKPOINT_PX)), "max-width")
    if viewport <= 640:  # phones: the figure cancels the page padding and drops its side borders
        phone = _media(640)
        fig = _css_block(r"\.hv-figure", phone)
        assert _px(fig, "margin-left") == -pad and _px(fig, "margin-right") == -pad
        assert "border-left: 0" in _css_block(r"\.hv-figure img", phone)
        return min(column + 2 * pad, cap)
    return min(column, cap) - 2 * border


def _svg_fonts(svg: str) -> tuple[float, list[float]]:
    width = float(re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg).group(1))
    sizes = [float(s) for s in re.findall(r"font-size: ([\d.]+)px", svg)]
    assert sizes, "SVG text is kept as text (svg.fonttype none)"
    return width, sizes


@pytest.mark.acceptance("AC-S8-07")
def test_page_serves_the_layout_matching_the_viewport(site, long_run):
    html = site.client.get(f"/runs/{long_run.run_id}?method=sdof").text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    _, per_method = _run(site, long_run.run_id)
    desktop = one(per_method["sdof"], kind="plot_svg")
    compact = one(per_method["sdof"], kind="plot_svg_compact")
    source = re.search(r"<source ([^>]+)>", html).group(1)
    assert f'media="(max-width: {pages.FIGURE_BREAKPOINT_PX}px)"' in source
    assert f'srcset="{compact["url"]}"' in source and 'type="image/svg+xml"' in source
    img = re.search(r'<img id="hv-plot" ([^>]+)>', html).group(1)
    assert f'src="{desktop["url"]}"' in img and 'alt="' in img
    assert compact["generated_by"]["recipe"]["layout"] == "compact"
    assert pages.COMPACT_MAX_PX == _px(
        _css_block(r"\.hv-figure img", _media(pages.FIGURE_BREAKPOINT_PX)), "max-width"
    )


@pytest.mark.acceptance("AC-S8-07")
def test_figure_text_is_at_least_9_css_px_on_every_viewport(site, short_run, long_run):
    worst = {}
    for pub in (short_run, long_run):
        _, per_method = _run(site, pub.run_id)
        for method, items in per_method.items():
            for layout, kind in (("desktop", "plot_svg"), ("compact", "plot_svg_compact")):
                width_units, sizes = _svg_fonts(download(site, one(items, kind=kind)["url"])[0].decode())
                vps = (
                    range(pages.FIGURE_BREAKPOINT_PX + 1, 2561)
                    if layout == "desktop"
                    else range(MIN_VIEWPORT_PX, pages.FIGURE_BREAKPOINT_PX + 1)
                )
                for vp in vps:
                    for sb in (0, SCROLLBAR_PX):
                        px = min(sizes) * rendered_width(vp, layout, sb) / width_units
                        key = (layout, method)
                        if key not in worst or px < worst[key][0]:
                            worst[key] = (px, vp, sb)
    for key, (px, vp, sb) in worst.items():
        assert px >= MIN_CSS_PX, f"{key}: {px:.2f} CSS px at a {vp} px viewport (scrollbar {sb})"
    # the binding cases, for the record: compact at the narrowest viewport, desktop just above the breakpoint
    assert worst[("compact", "sdof")][1] == MIN_VIEWPORT_PX
    assert worst[("desktop", "sdof")][1] == pages.FIGURE_BREAKPOINT_PX + 1


def _layout_problems(fig) -> list[str]:
    r = fig.canvas.get_renderer()
    fb, ax = fig.bbox, fig.axes[0]
    out = []
    tb = ax.get_tightbbox(r)
    if tb.x0 < -0.5 or tb.y0 < -0.5 or tb.x1 > fb.x1 + 0.5 or tb.y1 > fb.y1 + 0.5:
        out.append(f"axes decorations outside the figure: {tb}")
    for tx in fig.texts:
        if tx.get_text() and tx.get_visible():
            b = tx.get_window_extent(r)
            if b.x0 < fb.x0 - 0.5 or b.x1 > fb.x1 + 0.5 or b.y0 < fb.y0 - 0.5 or b.y1 > fb.y1 + 0.5:
                out.append(f"text clipped: {tx.get_text()[:40]!r}")
    if fig.legends:
        leg = fig.legends[0].get_window_extent(r)
        if leg.x1 > fb.x1 + 0.5 or leg.x0 < -0.5:
            out.append("legend outside the figure")
        foot_top = max(tx.get_window_extent(r).y1 for tx in fig.texts if tx.get_gid() == "footer")
        if leg.y0 < foot_top:
            out.append("legend overlaps the footer")
        if leg.y1 > ax.xaxis.label.get_window_extent(r).y0:
            out.append("legend overlaps the x label")
    title = next(tx for tx in fig.texts if tx.get_gid() == "title").get_window_extent(r)
    if title.y0 < ax.get_window_extent(r).y1:
        out.append("title overlaps the axes")
    return out


@pytest.mark.acceptance("AC-S8-07")
def test_figure_elements_never_overlap_or_clip(site, short_run, long_run):
    n = 0
    for pub in (short_run, long_run):
        _, per_method = _run(site, pub.run_id)
        docs = {m: _doc(site, items) for m, items in per_method.items()}
        for method, doc in docs.items():
            alt = "standard" if method == "sdof" else "sdof"
            for layout in ("desktop", "compact"):
                for display in ({}, {"show_alternate_method": True}):
                    recipe = plots.make_recipe(
                        doc, fmt="svg", layout=layout, display=display, alternate_method=alt
                    )
                    problems = _layout_problems(_figure(doc, recipe, docs[alt]))
                    assert not problems, (pub.record_code, method, layout, display, problems)
                    n += 1
    assert n == 16


@pytest.mark.acceptance("AC-S8-07")
def test_pages_are_responsive(site, short_run):
    phone = _media(640)
    assert "table.stack tr" in phone and "content: attr(data-label)" in phone, "tables become cards"
    assert "grid-template-columns: 1fr" in phone, "key-value lists stack"
    fixed = [int(w) for w in re.findall(r"(?<![-\w])width:\s*(\d+)px", CSS)]
    assert all(w <= MIN_VIEWPORT_PX for w in fixed), fixed
    assert "overflow-x: auto" in _css_block(r"\.table-scroll", CSS)
    for url in (
        "/",
        "/upload",
        "/data",
        f"/measurements/{short_run.measurement_id}",
        f"/runs/{short_run.run_id}",
    ):
        html = site.client.get(url).text
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
        assert '<link rel="stylesheet" href="/static/style.css">' in html
        for table in re.findall(r"<table[^>]*>", html):
            assert "stack" in table, table
        for row in re.findall(r"<tbody>(.*?)</tbody>", html, re.S):
            for td in re.findall(r"<td[^>]*>", row):
                assert "data-label=" in td, td
    assert site.client.get("/static/style.css").status_code == 200
    evidence = ROOT / "docs/evidence/stage8"
    assert {p.name for p in evidence.glob("*.png")} >= {"run-page-340px-compact.png", "run-page-desktop.png"}


# --------------------------------------------------------------------------------------- AC-S8-11
@pytest.mark.acceptance("AC-S8-11")
def test_results_title_is_record_code_with_coordinates(site, short_run, long_run):
    for pub, coords in ((short_run, "(19.4326, -99.1332)"), (long_run, "(40.416775, -3.70379)")):
        title = f"{pub.record_code} {coords}"
        assert re.fullmatch(r"[0-9a-z]{5}-\d{4}\.\d{4} \(-?\d+(\.\d+)?, -?\d+(\.\d+)?\)", title)
        html = site.client.get(f"/runs/{pub.run_id}").text
        assert element_text(html, "results-title") == title
        assert re.search(r"<title>(.*?)</title>", html).group(1).startswith(title)
        _, per_method = _run(site, pub.run_id)
        for items in per_method.values():
            for kind in ("plot_svg", "plot_svg_compact"):
                art = one(items, kind=kind)
                svg = download(site, art["url"])[0].decode()
                assert art["generated_by"]["recipe"]["title"] == title
                assert re.search(r"<title>(.*?)</title>", svg).group(1) == title
                assert title in svg_texts(svg)
        assert site.client.get(f"/measurements/{pub.measurement_id}").text.count(f"<h1>{title}</h1>") == 1


@pytest.mark.acceptance("AC-S8-11")
def test_one_band_labeled_exactly_when_estimable(site, long_run):
    _, per_method = _run(site, long_run.run_id)
    docs = {m: _doc(site, items) for m, items in per_method.items()}
    for method, items in per_method.items():
        doc = docs[method]
        assert doc["ci_label"] == BAND_LABEL
        art = one(items, kind="plot_svg")
        recipe = art["generated_by"]["recipe"]
        assert recipe["band"] == {
            "label": BAND_LABEL,
            "drawn": True,
            "status": "ci_estimated",
            "reason": None,
        }
        alt = "standard" if method == "sdof" else "sdof"
        for display in ({}, {"show_alternate_method": True}):
            r = plots.make_recipe(doc, fmt="svg", display=display, alternate_method=alt)
            fig = _figure(doc, r, docs[alt])
            bands = fig.axes[0].findobj(PolyCollection)
            assert (
                len(bands) == 1 and bands[0].get_label() == BAND_LABEL and bands[0].get_gid() == "ci95-band"
            )
            legend = [t.get_text() for t in fig.legends[0].get_texts()]
            assert legend.count(BAND_LABEL) == 1, legend
        svg = download(site, art["url"])[0].decode()
        assert svg_texts(svg).count(BAND_LABEL) == 1 and svg.count('id="ci95-band"') == 1
        html = site.client.get(f"/runs/{long_run.run_id}?method={method}").text
        note = element_text(html, "ci-note")
        unc = plots.main_selection(doc)["uncertainty"]
        assert BAND_LABEL in note and "selection-conditional" in note and unc["method"] in note
        assert "not calibrated" in note


@pytest.mark.acceptance("AC-S8-11")
def test_no_band_when_not_estimable_and_the_reason_is_shown(site, short_run):
    _, per_method = _run(site, short_run.run_id)
    for method, items in per_method.items():
        doc = _doc(site, items)
        art = one(items, kind="plot_svg")
        recipe = art["generated_by"]["recipe"]
        unc = plots.main_selection(doc, recipe["selection_id"])["uncertainty"]
        assert recipe["band"]["drawn"] is False and recipe["band"]["reason"] == unc["status_reason"]
        assert not _figure(doc, recipe).axes[0].findobj(PolyCollection)
        svg = download(site, art["url"])[0].decode()
        assert BAND_LABEL not in svg_texts(svg) and 'id="ci95-band"' not in svg
        note = element_text(site.client.get(f"/runs/{short_run.run_id}?method={method}").text, "ci-note")
        assert unc["status"] in note and unc["status_reason"] in note


def test_layout_constants_documented():
    src = Path(pages.__file__).read_text()
    assert "AC-S8-07" in src and pages.FIGURE_BREAKPOINT_PX == 900 and pages.COMPACT_MAX_PX == 480
