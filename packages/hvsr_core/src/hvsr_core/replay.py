"""Offline replay of a published analysis: undo the reporting layer, rebuild the record identity
from the document, and run `analysis.verify_replay` against the original recording."""

from __future__ import annotations

import copy

from . import sesame
from .analysis import RecordInfo, verify_replay
from .config import AnalysisConfig, AnalysisRequest
from .model import Recording


def science_view(doc: dict) -> dict:
    """The scientific document behind a published one.

    Publication adds licensing, report options (SESAME evaluation), the robustness link, and
    candidate-table URLs; this restores their science-document defaults so replay compares science.
    """
    d = copy.deepcopy(doc)
    d["licensing"] = None
    d["sesame_requested"] = False
    d["robustness_comparison_url"] = None
    d["windows"]["candidates_url"] = None
    d["quality_dimensions"]["sensitivity"]["robustness"] = {"status": "not_run"}
    for sel in d["selections"]:
        sel["sesame"] = sesame.not_requested()
    return d


def record_info(doc: dict) -> RecordInfo:
    prov = doc["provenance"]
    hashes = {k: v for k, v in (prov.get("input_hashes") or {}).items() if k != "recording_content_sha256"}
    return RecordInfo(
        **doc["record"],
        input_hashes=hashes,
        response_sha256=prov.get("response_sha256"),
        instrument=doc["instrument"],
    )


def group_run(docs: list[dict]) -> dict[str, dict]:
    """Published documents of one run, keyed by method; refuses mixed runs or duplicates."""
    by_method: dict[str, dict] = {}
    runs = {d["run_id"] for d in docs}
    if len(runs) != 1:
        raise ValueError(f"documents belong to {len(runs)} different runs: {sorted(runs)}")
    for d in docs:
        if d["method"] in by_method:
            raise ValueError(f"two documents for method {d['method']!r}")
        by_method[d["method"]] = d
    return by_method


def replay_published(rec: Recording, docs: list[dict]) -> dict:
    """Recompute from `rec` and compare with every published method document of the run."""
    by_method = group_run(docs)
    first = next(iter(by_method.values()))
    request = AnalysisRequest(config=AnalysisConfig.model_validate(first["config"]))
    report = verify_replay(
        rec, request, {m: science_view(d) for m, d in by_method.items()}, record_info(first)
    )
    return {"methods": sorted(by_method), "published_run_id": first["run_id"], **report}
