"""Generate the configuration reference, config JSON Schema, and data dictionary from the code."""

from __future__ import annotations

import json
from importlib import resources
from typing import Literal, get_args, get_origin

from pydantic import BaseModel

from .config import AnalysisRequest
from .schema_validation import config_schema


def _walk(model: type[BaseModel], prefix: str, rows: list[tuple[str, str, str, str]]) -> None:
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        ann = field.annotation
        sub = None
        for cand in getattr(ann, "__args__", ()) or (ann,):
            if isinstance(cand, type) and issubclass(cand, BaseModel):
                sub = cand
        if field.default_factory is not None:
            default = "(see nested fields)" if sub else repr(field.default_factory())
        else:
            default = repr(field.default)
        if get_origin(ann) is Literal:
            type_txt = "one of " + ", ".join(repr(a) for a in get_args(ann))
        else:
            type_txt = getattr(ann, "__name__", None) or str(ann).replace("typing.", "").replace(
                "hvsr_core.config.", ""
            )
        rows.append((path, type_txt, default, field.description or ""))
        if sub is not None:
            _walk(sub, path + ".", rows)


def configuration_markdown() -> str:
    rows: list[tuple[str, str, str, str]] = []
    _walk(AnalysisRequest, "", rows)
    lines = [
        "# Configuration reference",
        "",
        "Generated from `hvsr_core.config` by `uv run hvsr docs`. Do not edit by hand.",
        "",
        "Defaults are initial, editable settings, not universally suitable values. Fields under",
        "`config.` are scientific and enter the scientific configuration hash. `report.`, `display.`, and",
        "`robustness_*` do not.",
        "",
        "| Field | Type | Default | Description |",
        "| --- | --- | --- | --- |",
    ]
    for path, typ, default, desc in rows:
        typ = typ.replace("|", "\\|")
        default = default.replace("|", "\\|")
        desc = desc.replace("|", "\\|")
        lines.append(f"| `{path}` | `{typ}` | `{default}` | {desc} |")
    return "\n".join(lines) + "\n"


def config_schema_text() -> str:
    return json.dumps(config_schema(), indent=2, sort_keys=True) + "\n"


def packaged_config_schema_text() -> str:
    return resources.files("hvsr_core.schemas").joinpath("config.schema.json").read_text()


def _cell(text) -> str:
    return str(text).replace("|", "\\|")


def data_dictionary_markdown() -> str:
    from .exports import data_dictionary

    dd = data_dictionary()
    lines = [
        "# Data dictionary",
        "",
        "Generated from `hvsr_core.exports.data_dictionary()` by `scripts/gen_docs.py`. Do not edit by hand.",
        "The same content is served as JSON at `GET /api/v1/data-dictionary` (schema `data_dictionary`).",
        "",
        f"Schema version {dd['schema_version']}; export format `{dd['export_format']}`.",
        "",
        "## CSV conventions",
        "",
        "These apply to every numeric table below.",
        "",
        "| Convention | Rule |",
        "| --- | --- |",
        *(f"| {_cell(k)} | {_cell(v)} |" for k, v in dd["csv_conventions"].items()),
        "",
        "Waveform files are not numeric tables. A retained original is served byte for byte, whatever its",
        "own layout. A Reconstructed CSV (canonical-only storage) begins with one `#` line stating that the",
        "original bytes were not retained, then the original's preserved comment lines, header, and",
        "delimiter; formatting outside the preservation contract is not reproduced. Use the",
        "`normalized_waveform` table for a fixed layout.",
        "",
    ]
    for name, table in dd["tables"].items():
        lines += [
            f"## `{name}`: {table['title']}",
            "",
            f"Each row is {table['row']}.",
            "",
            table["description"],
            "",
            "| Column | Type | Unit | Nullable | Description |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in table["columns"]:
            desc = c["description"]
            if c.get("enum"):
                desc += " One of: " + ", ".join(f"`{v}`" for v in c["enum"]) + "."
            lines.append(
                f"| `{c['name']}` | {c['type']} | {_cell(c['unit'] or '')} | "
                f"{'yes' if c['nullable'] else 'no'} | {_cell(desc)} |"
            )
        if table.get("optional_columns"):
            lines += ["", "Optional columns: " + ", ".join(f"`{c}`" for c in table["optional_columns"]) + "."]
        for dyn in table.get("dynamic_columns", []):
            lines += ["", f"Columns matching `{dyn['pattern']}` ({dyn['type']}): {dyn['description']}"]
        lines.append("")
    lines += [
        "## JSON documents",
        "",
        "| Document | Description |",
        "| --- | --- |",
        *(f"| `{k}` | {_cell(v)} |" for k, v in dd["json_documents"].items()),
        "",
        "## Units used in the analysis JSON",
        "",
        "| Quantity | Unit |",
        "| --- | --- |",
        *(f"| `{k}` | {_cell(v)} |" for k, v in dd["analysis_units"].items()),
        "",
        "## Kinds of content",
        "",
        "| Kind | Manifest categories |",
        "| --- | --- |",
        *(f"| {k} | {_cell(v)} |" for k, v in dd["categories"].items()),
    ]
    return "\n".join(lines) + "\n"
