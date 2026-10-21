"""Stage 7 fixtures: an isolated migrated database and object store per test, a controllable clock."""

from __future__ import annotations

import pytest

from hvsr_web.context import AppContext
from hvsr_web.settings import Settings
from hvsr_web.storage import LocalObjectStore

from stage7_support import SIGNING_KEY, Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_ctx(tmp_path, clock):
    def make(name: str = "main", store=None, **overrides) -> AppContext:
        s = Settings(
            database_url=f"sqlite:///{tmp_path / name}.sqlite3",
            local_store_dir=str(tmp_path / f"{name}-objects"),
            url_signing_key=SIGNING_KEY,
            **overrides,
        )
        if store is None:
            store = LocalObjectStore(s.local_store_dir, SIGNING_KEY.encode(), clock=clock.epoch)
        ctx = AppContext.from_settings(s, store=store)
        ctx.clock = clock
        return ctx

    return make


@pytest.fixture
def ctx(make_ctx) -> AppContext:
    return make_ctx()
