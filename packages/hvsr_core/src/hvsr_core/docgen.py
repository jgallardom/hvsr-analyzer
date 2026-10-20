"""Generate the configuration reference and config JSON Schema from the pydantic models."""

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
            type_txt = getattr(ann, "__name__", None) or str(ann).replace("typing.", "").replace("hvsr_core.config.", "")
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
