from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import sessionmaker

from .db import make_engine, make_session_factory, migrate
from .settings import Settings
from .storage import ObjectStore, make_store


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class AppContext:
    settings: Settings
    sessions: sessionmaker
    store: ObjectStore
    clock: Callable[[], dt.datetime] = field(default=utc_now)

    @classmethod
    def from_settings(cls, settings: Settings, store: ObjectStore | None = None, run_migrations: bool = True):
        if run_migrations:
            migrate(settings.database_url)
        return cls(
            settings, make_session_factory(make_engine(settings.database_url)), store or make_store(settings)
        )
