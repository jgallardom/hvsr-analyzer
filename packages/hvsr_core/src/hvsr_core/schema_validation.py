"""JSON Schema validation for published documents, plus contract checks schemas cannot express."""

from __future__ import annotations

import json
import math
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .config import AnalysisRequest

SCHEMA_FILES = {
    "analysis": "analysis.schema.json",
    "manifest": "manifest.schema.json",
    "template_bank": "template_bank.schema.json",
    "config": "config.schema.json",
    "window_table": "window_table.schema.json",
    "plot_recipe": "plot_recipe.schema.json",
    "data_dictionary": "data_dictionary.schema.json",
}


class ContractError(ValueError):
    pass


@cache
def load_schema(name: str) -> dict:
    if name == "config":
        return config_schema()
    text = resources.files("hvsr_core.schemas").joinpath(SCHEMA_FILES[name]).read_text()
    return json.loads(text)


def config_schema() -> dict:
    schema = AnalysisRequest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://hvsr-analyzer.example/schemas/config/2.0.0.json"
    return schema


def _contract_errors(obj: Any, path: str = "$") -> list[str]:
    errs: list[str] = []
    if isinstance(obj, float) and not math.isfinite(obj):
        errs.append(f"{path}: non-finite number")
    elif isinstance(obj, str):
        s = obj.strip()
        if len(s) >= 2 and ((s[0] == "{" and s[-1] == "}") or (s[0] == "[" and s[-1] == "]")):
            try:
                json.loads(s)
                errs.append(f"{path}: JSON encoded inside a string field")
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            errs.extend(_contract_errors(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            errs.extend(_contract_errors(v, f"{path}[{i}]"))
    return errs


def validation_errors(doc: Any, name: str) -> list[str]:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errs = [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in validator.iter_errors(doc)]
    errs.extend(_contract_errors(doc))
    return errs


def validate(doc: Any, name: str) -> None:
    errs = validation_errors(doc, name)
    if errs:
        raise ContractError(f"{name} document invalid: " + "; ".join(errs[:20]))
