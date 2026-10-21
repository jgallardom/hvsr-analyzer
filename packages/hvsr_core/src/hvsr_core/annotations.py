"""Attributed human annotations: append-only, stored separately, never written into computed documents."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .jsonutil import canonical_hash

KINDS = ("interpretation", "comment", "data_quality_flag")


class AnnotationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(min_length=1)
    method: Literal["standard", "sdof"] | None = None
    selection_id: str | None = None
    peak_family_id: str | None = None


class Annotation(BaseModel):
    """Public attribution only; private identity is held by the account store, not here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    annotation_id: str
    target: AnnotationTarget
    kind: Literal["interpretation", "comment", "data_quality_flag"]
    text: str = Field(min_length=1, max_length=5000)
    interpreted_f0_hz: float | None = Field(default=None, gt=0)
    attribution: str = Field(min_length=1, max_length=200)
    created_utc: str
    supersedes: str | None = None


class AnnotationLog:
    """Append-only. Superseding adds a new record; earlier records stay visible."""

    def __init__(self) -> None:
        self._items: list[Annotation] = []

    def add(
        self,
        target: AnnotationTarget,
        kind: str,
        text: str,
        attribution: str,
        interpreted_f0_hz: float | None = None,
        supersedes: str | None = None,
        created_utc: str | None = None,
    ) -> Annotation:
        if supersedes is not None and supersedes not in {a.annotation_id for a in self._items}:
            raise KeyError(f"unknown annotation {supersedes}")
        created = created_utc or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        body = {
            "target": target.model_dump(),
            "kind": kind,
            "text": text,
            "attribution": attribution,
            "interpreted_f0_hz": interpreted_f0_hz,
            "created_utc": created,
            "supersedes": supersedes,
            "seq": len(self._items),
        }
        ann = Annotation(
            annotation_id="a" + canonical_hash(body)[:20], **{k: v for k, v in body.items() if k != "seq"}
        )
        self._items.append(ann)
        return ann

    def for_run(self, run_id: str) -> tuple[Annotation, ...]:
        return tuple(a for a in self._items if a.target.run_id == run_id)

    def __len__(self) -> int:
        return len(self._items)


def merge_for_display(doc: dict, annotations: tuple[Annotation, ...] | list[Annotation]) -> dict:
    """View model: computed document copied unchanged; annotations in a separate key."""
    superseded = {a.supersedes for a in annotations if a.supersedes}
    return {
        "computed": copy.deepcopy(doc),
        "computed_hash": canonical_hash(doc),
        "annotations": [
            {**a.model_dump(), "source": "human", "current": a.annotation_id not in superseded}
            for a in annotations
            if a.target.run_id == doc["run_id"]
        ],
    }
