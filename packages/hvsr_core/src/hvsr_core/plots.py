"""Static figures (mpl-static-v1), rendered only from a published analysis document.

Main plot (spec 13.1): linear frequency in Hz on x, logarithmic positive H/V on y; the observed
selected-window geometric mean; ONE shaded band, the nominal 95% pointwise CI, drawn only when it is
estimable; the observed main-peak marker and a separate maximum marker when it differs. Optional
overlays use stored numbers only (the other method's mean curve; selected windows' spectra from the
per-window spectra export). A compact provenance footer sits below the axes and full details stay
in the JSON. SESAME criteria are never drawn or explained in the figure. A cosmetic change is a new
recipe rendered from the same numbers, never a new computation.
"""

from __future__ import annotations

import csv
import gzip
import io
import math
import textwrap

import matplotlib
import numpy as np
from matplotlib import rc_context
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from .jsonutil import canonical_hash
from .strings import results_title, t
from .versions import ALGORITHM_VERSIONS, PACKAGE_VERSION, SCHEMA_VERSIONS

RENDERING_VERSION = ALGORITHM_VERSIONS["plotting"]
FONT = "DejaVu Sans"  # shipped with matplotlib: identical metrics on every platform
DEFAULT_DPI = 150
LAYOUTS = {
    "desktop": {"font_pt": 10.0, "footer_pt": 7.5},
    "compact": {"font_pt": 9.0, "footer_pt": 7.5, "width_in": 3.6, "height_in": 6.4},
}
COLORS = {
    "mean": "#1f4e9c",
    "band": "#7fa7e0",
    "alternate": "#b5541b",
    "windows": "#9a9a9a",
    "peak": "#c0392b",
}
# No math text: superscript exponents would shrink below the readable size, and record-derived strings
# must render literally.
RC = {
    "font.family": FONT,
    "svg.fonttype": "none",
    "text.parse_math": False,
    "axes.linewidth": 0.8,
    "path.simplify": False,
}
MINOR_LABEL_MAX_DECADES = 1.5


def _arr(values) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def _hz(v: float) -> str:
    return f"{v:.3f}"


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else t("value.unknown")


def _log_ticks(ax) -> None:
    """Plain-number H/V tick labels at full tick size; 2x and 5x minor ticks are labeled on short ranges."""

    def minor(v, _pos) -> str:
        lo, hi = ax.get_ylim()
        if v <= 0 or lo <= 0 or math.log10(hi / lo) > MINOR_LABEL_MAX_DECADES:
            return ""
        mantissa = round(v / 10 ** math.floor(math.log10(v) + 1e-9))
        return f"{v:g}" if mantissa in (2, 5) else ""

    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:g}"))
    ax.yaxis.set_minor_formatter(FuncFormatter(minor))


def main_selection(doc: dict, selection_id: str | None = None) -> dict:
    sels = doc["selections"]
    if not sels:
        raise KeyError(f"{doc['method']} has no selection ({doc['method_status']}): nothing to plot")
    if selection_id is not None:
        return next(s for s in sels if s["selection_id"] == selection_id)
    fam = doc["interpretation"].get("fundamental_family_id")
    return next((s for s in sels if fam and s["peak_family_id"] == fam), None) or next(
        (s for s in sels if s["status"] == "computed"), sels[0]
    )


def plotted_series(doc: dict, sel: dict) -> dict:
    mc = sel["mean_curve"]
    return {
        "frequency_hz": _arr(doc["grid"]["frequency_hz"]),
        "hv_geometric_mean": _arr(mc["hv_geometric_mean"]),
        "ci95_lower": _arr(mc["ci95_lower"]),
        "ci95_upper": _arr(mc["ci95_upper"]),
    }


def series_sha256(series: dict) -> str:
    """Checksum of the plotted numbers (NaN as null): equal for the plot, the CSV, and the JSON."""
    return canonical_hash(
        {k: [None if not np.isfinite(v) else float(v) for v in a] for k, a in series.items()}
    )


def band_drawn(series: dict) -> bool:
    return bool(np.any(np.isfinite(series["ci95_lower"]) & np.isfinite(series["ci95_upper"])))


def footer_lines(doc: dict, sel: dict, drawn: bool, width_chars: int) -> list[str]:
    cfg, rec, unc = doc["config"], doc["recording"], sel["uncertainty"]
    record = doc["record"]
    bp = cfg["preprocessing"]["bandpass"]
    prep = t(
        "fig.preprocessing",
        detrend=cfg["preprocessing"]["detrend"],
        taper=_num(cfg["preprocessing"]["taper_alpha"]),
        bandpass=t("value.none")
        if bp is None
        else t("fig.bandpass", low=_num(bp["low_hz"]), high=_num(bp["high_hz"])),
        response=cfg["preprocessing"]["response_correction"],
    )
    bank = doc["provenance"].get("template_bank")
    lines = [
        t(
            "fig.identity",
            title=results_title(record["record_code"], record["latitude"], record["longitude"]),
            run_id=doc["run_id"],
            method=t(f"method.{doc['method']}"),
            family=sel["peak_family_id"] or t("value.none"),
        ),
        t(
            "fig.windows",
            lw=_num(rec["window_length_s"]),
            step=_num(rec["step_s"]),
            n=sel["n_selected"],
            b=_num(cfg["spectral"]["ko_bandwidth"]),
            fmin=_num(cfg["peak_search"]["fmin_hz"]),
            fmax=_num(cfg["peak_search"]["fmax_hz"]),
            tmin=_num(rec["analyzed_start_s"]),
            tmax=_num(rec["analyzed_end_s"]),
            combination=t(f"combination.{cfg['spectral']['horizontal_combination']}"),
        ),
        t(
            "fig.processing",
            prep=prep,
            instrument=t(f"instrument.{doc['instrument']['status']}"),
            version=doc["provenance"]["software_version"],
            rendering=RENDERING_VERSION,
            bank=t("fig.bank", bank_id=bank["bank_id"]) if bank else "",
        ),
        t(
            "fig.ci",
            label=doc["ci_label"],
            method=unc.get("method"),
            replicates=unc.get("n_bootstrap"),
            blocks=unc.get("informative_blocks"),
            block_s=_num(unc.get("block_length_s")),
            calibration=t(f"calibration.{doc['calibration']['status']}"),
        )
        if drawn
        else t("fig.ci_none", status=unc.get("status"), reason=unc.get("status_reason") or ""),
    ]
    out = []
    for line in lines:
        out += textwrap.wrap(line, width_chars, break_on_hyphens=False) or [""]
    return out


def figure_size_in(display: dict, layout: str = "desktop") -> tuple[float, float]:
    lay = LAYOUTS[layout]
    return (
        float(lay.get("width_in", display.get("figure_width_in", 9.0))),
        float(lay.get("height_in", display.get("figure_height_in", 6.0))),
    )


def raster_pixels(display: dict, layout: str = "desktop") -> int:
    """Pixels of a PNG rendered with these display options (memory grows with this, 4 bytes each)."""
    w, h = figure_size_in(display, layout)
    dpi = int(display.get("dpi", DEFAULT_DPI))
    return round(w * dpi) * round(h * dpi)


def make_recipe(
    doc: dict,
    *,
    fmt: str,
    layout: str = "desktop",
    display: dict | None = None,
    selection_id: str | None = None,
    alternate_method: str | None = None,
    windows_available: bool = False,
    source_sha256: str | None = None,
) -> dict:
    """The complete, stored description of one figure (schema plot_recipe)."""
    display = display or {}
    lay = LAYOUTS[layout]
    width, height = figure_size_in(display, layout)
    sel = main_selection(doc, selection_id)
    show_windows = bool(display.get("show_individual_windows", False))
    show_alt = bool(display.get("show_alternate_method", False)) and alternate_method is not None
    series = plotted_series(doc, sel)
    drawn = band_drawn(series)
    footer_chars = int(width * 72 / (0.56 * lay["footer_pt"])) - 2
    return {
        "schema_id": "hvsr-analyzer/plot-recipe",
        "schema_version": SCHEMA_VERSIONS["plot_recipe"],
        "rendering_version": RENDERING_VERSION,
        "renderer": {"software": f"hvsr-analyzer {PACKAGE_VERSION}", "matplotlib": matplotlib.__version__},
        "run_id": doc["run_id"],
        "method": doc["method"],
        "selection_id": sel["selection_id"],
        "peak_family_id": sel["peak_family_id"],
        "source_analysis_sha256": source_sha256,
        "format": fmt,
        "layout": layout,
        "width_in": float(width),
        "height_in": float(height),
        "dpi": int(display.get("dpi", DEFAULT_DPI)),
        "font_pt": lay["font_pt"],
        "footer_pt": lay["footer_pt"],
        "x_scale": "linear",
        "y_scale": "log",
        "x_range_hz": [0.0, float(doc["config"]["peak_search"]["fmax_hz"])],
        "title": results_title(
            doc["record"]["record_code"], doc["record"]["latitude"], doc["record"]["longitude"]
        ),
        "band": {
            "label": doc["ci_label"],
            "drawn": drawn,
            "status": sel["uncertainty"].get("status"),
            "reason": None if drawn else sel["uncertainty"].get("status_reason"),
        },
        "show_alternate_method": show_alt,
        "alternate_method": alternate_method if show_alt else None,
        "show_individual_windows": show_windows,
        "individual_windows_rendered": show_windows and windows_available,
        "individual_windows_note": None
        if not show_windows or windows_available
        else t("plot.windows_unavailable"),
        "footer": footer_lines(doc, sel, drawn, footer_chars),
        "series_sha256": series_sha256(series),
    }


def window_curves(window_spectra_gz: bytes, window_ids: list[str], freq: np.ndarray) -> dict[str, np.ndarray]:
    """Selected windows' H/V on the analysis grid, read from the per-window spectra export."""
    wanted = set(window_ids)
    index = {float(f): i for i, f in enumerate(freq)}
    out = {w: np.full(freq.size, np.nan) for w in window_ids}
    rows = csv.reader(io.StringIO(gzip.decompress(window_spectra_gz).decode("utf-8")))
    next(rows)
    for wid, _start, f, hv, _why in rows:
        if wid in wanted and hv:
            out[wid][index[float(f)]] = float(hv)
    return out


def build_figure(
    doc: dict,
    recipe: dict,
    *,
    alternate_doc: dict | None = None,
    windows: dict[str, np.ndarray] | None = None,
) -> tuple[Figure, dict]:
    """The figure and the exact arrays handed to matplotlib."""
    sel = main_selection(doc, recipe["selection_id"])
    series = plotted_series(doc, sel)
    f = series["frequency_hz"]
    fp, footer_pt = recipe["font_pt"], recipe["footer_pt"]
    w_in, h_in = recipe["width_in"], recipe["height_in"]
    fig = Figure(figsize=(w_in, h_in), dpi=recipe["dpi"])
    ax = fig.add_axes((0.1, 0.1, 0.8, 0.8))
    if windows and recipe["individual_windows_rendered"]:
        for k, (wid, hv) in enumerate(sorted(windows.items())):
            ax.plot(
                f,
                hv,
                color=COLORS["windows"],
                lw=0.5,
                zorder=1,
                label=t("plot.windows", n=len(windows)) if k == 0 else None,
                gid=f"window-{wid}",
            )
    if recipe["band"]["drawn"]:
        ok = np.isfinite(series["ci95_lower"]) & np.isfinite(series["ci95_upper"])
        ax.fill_between(
            f,
            series["ci95_lower"],
            series["ci95_upper"],
            where=ok,
            color=COLORS["band"],
            alpha=0.45,
            lw=0,
            zorder=2,
            label=doc["ci_label"],
            gid="ci95-band",
        )
    if alternate_doc is not None and recipe["show_alternate_method"]:
        alt = plotted_series(alternate_doc, main_selection(alternate_doc))
        ax.plot(
            alt["frequency_hz"],
            alt["hv_geometric_mean"],
            color=COLORS["alternate"],
            lw=1.0,
            ls="--",
            zorder=3,
            label=t("plot.alternate", method=t(f"method.{alternate_doc['method']}")),
        )
    ax.plot(
        f,
        series["hv_geometric_mean"],
        color=COLORS["mean"],
        lw=1.6,
        zorder=4,
        gid="mean-curve",
        label=t(f"plot.mean.{doc['method']}", family=sel["peak_family_id"] or t("value.none")),
    )
    peak = next((p for p in sel["peaks"] if p["peak_family_id"] == sel["peak_family_id"]), None)
    pf = None
    if peak is not None and peak["status"] == "peak_found":
        pf = peak["observed_peak_frequency_hz"]
        ax.plot(
            [pf],
            [peak["observed_peak_amplitude"]],
            ls="none",
            marker="v",
            ms=7,
            color=COLORS["peak"],
            zorder=5,
            label=t("plot.peak", f=_hz(pf)),
            gid="observed-peak",
        )
    mx = sel["maximum"]
    if mx["maximum_frequency_hz"] is not None and (
        pf is None or abs(mx["maximum_frequency_hz"] - pf) > 1e-9 * pf
    ):
        ax.plot(
            [mx["maximum_frequency_hz"]],
            [mx["maximum_amplitude"]],
            ls="none",
            marker="o",
            ms=6,
            mfc="none",
            mec="black",
            mew=1.0,
            zorder=5,
            label=t("plot.maximum", f=_hz(mx["maximum_frequency_hz"])),
            gid="maximum",
        )
    ax.set_xscale("linear")
    ax.set_yscale("log")
    ax.set_xlim(*recipe["x_range_hz"])
    if not np.any(np.isfinite(series["hv_geometric_mean"])):
        ax.set_ylim(0.1, 10.0)
        ax.text(
            0.5,
            0.5,
            t("plot.no_data", reason=sel["status_reason"] or sel["status"]),
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=fp,
            wrap=True,
        )
    _log_ticks(ax)
    ax.set_xlabel(t("plot.x_label"), fontsize=fp)
    ax.set_ylabel(t("plot.y_label"), fontsize=fp)
    ax.tick_params(which="both", labelsize=fp - 0.5)
    ax.grid(True, which="both", lw=0.3, alpha=0.5)

    # Vertical stack from the bottom: footer, legend (outside the axes, never over data), axes, title.
    line_in = footer_pt * 1.35 / 72
    y = 0.05
    for line in reversed(recipe["footer"]):
        fig.text(
            0.01, y / h_in, line, fontsize=footer_pt, ha="left", va="bottom", color="#333333", gid="footer"
        )
        y += line_in
    handles, labels = ax.get_legend_handles_labels()
    legend_pt = fp - 1.5
    left_in, right_in = fp * 5.2 / 72, 0.15
    ncol, legend_x = (2, left_in) if recipe["layout"] == "desktop" else (1, 0.04)
    # The band label is kept on one line so it reads exactly as specified; other labels may wrap.
    wrap = int(((w_in - legend_x - 0.05) * 72 / ncol - 3.2 * legend_pt) / (0.58 * legend_pt))
    labels = [lb if lb == doc["ci_label"] else "\n".join(textwrap.wrap(lb, wrap)) for lb in labels]
    rows = -(-len(labels) // ncol)
    columns = [labels[k * rows : (k + 1) * rows] for k in range(ncol)]
    legend_in = (
        (max((sum(1.25 * (lb.count("\n") + 1) + 0.5 for lb in col) for col in columns), default=0) + 0.8)
        * legend_pt
        / 72
    )
    legend_top = y + 0.06 + legend_in
    xlabel_in = fp * 3.6 / 72
    title_in = (fp + 1.5) * 2.2 / 72
    bottom = (legend_top + xlabel_in) / h_in
    ax.set_position((left_in / w_in, bottom, 1 - (left_in + right_in) / w_in, 1 - bottom - title_in / h_in))
    if handles:
        fig.legend(
            handles,
            labels,
            loc="upper left",
            bbox_to_anchor=(legend_x / w_in, legend_top / h_in),
            ncol=ncol,
            fontsize=legend_pt,
            frameon=False,
            borderaxespad=0.0,
            borderpad=0.2,
            handlelength=1.6,
            columnspacing=1.2,
        )
    fig.text(
        0.5,
        1 - 0.06 / h_in,
        recipe["title"],
        ha="center",
        va="top",
        fontsize=fp + 1.5,
        fontweight="bold",
        gid="title",
    )
    return fig, series


def render(
    doc: dict,
    recipe: dict,
    *,
    alternate_doc: dict | None = None,
    windows: dict[str, np.ndarray] | None = None,
) -> bytes:
    with rc_context(dict(RC, **{"svg.hashsalt": recipe["series_sha256"]})):
        fig, _ = build_figure(doc, recipe, alternate_doc=alternate_doc, windows=windows)
        desc = t(
            "plot.metadata", run_id=recipe["run_id"], method=recipe["method"], sha=recipe["series_sha256"]
        )
        creator = f"hvsr-analyzer {PACKAGE_VERSION} ({RENDERING_VERSION})"
        meta = (
            {"Title": recipe["title"], "Description": desc, "Creator": creator, "Date": None}
            if recipe["format"] == "svg"
            else {"Title": recipe["title"], "Description": desc, "Software": creator}
        )
        buf = io.BytesIO()
        fig.savefig(buf, format=recipe["format"], dpi=recipe["dpi"], metadata=meta)
    return buf.getvalue()
