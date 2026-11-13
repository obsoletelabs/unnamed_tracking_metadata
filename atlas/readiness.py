"""Readiness: can this process take traffic now? Distinct from liveness (`/health`), which never touches
the database. Shared by `/api/v2/ready`, the admin `/status` detail and the `atlas_ready*` metrics so they
cannot disagree."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, media
from .settings import Settings


@dataclass(frozen=True)
class Readiness:
    database: bool   # the database answers a trivial query
    schema: str      # "current" | "behind" | "unknown" (db.schema_state); "unknown" when the database is down
    media: bool      # the artwork directory is writable
    frozen: bool = False      # an authority restored from a backup that has not resumed its feed (C16 §6.4)
    upgrading: bool = False   # the upgrade journal has unfinished stages (C16 §6.3)

    @property
    def status(self) -> str:
        """`not_ready` (HTTP 503): the database does not answer or migrations are pending. `degraded`
        (HTTP 200): serving, but artwork cannot be stored. `ready` otherwise. An `unknown` schema stays
        ready, as before: the check could not tell, and failing closed would take every pod out of
        rotation on a packaging fault. The metrics expose the unknown state."""
        if not self.database or self.schema == "behind":
            return "not_ready"
        return "ready" if self.media and not self.frozen and not self.upgrading else "degraded"

    @property
    def serving(self) -> bool:
        return self.status != "not_ready"


def check(session: Session, settings: Settings) -> Readiness:
    """Never raises: a failing dependency is a result, not an error."""
    try:
        session.execute(select(1))
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return Readiness(database=False, schema="unknown", media=media.storage_problem(settings) is None)
    from . import feed, upgrade

    try:
        frozen, upgrading = feed.frozen(session) is not None, upgrade.unfinished(session)
    except Exception:  # noqa: BLE001 - a table that cannot be read is a schema problem, reported as such above
        try:
            session.rollback()
        except Exception:
            pass
        frozen = upgrading = False
    return Readiness(database=True, schema=db.schema_state(session, settings.database_url),
                     media=media.storage_problem(settings) is None, frozen=frozen, upgrading=upgrading)
