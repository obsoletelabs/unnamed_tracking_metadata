"""The "Getting started" card on /admin (SPEC-UI-POLISH-PASS §3.7).

Every item is computed from what the node already stores; nothing here is written except the per-node
"hidden" marker. A standalone local node with no peers and no group is a complete setup: the optional
section ("Connect to other nodes") is never counted as unfinished.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import backup
from ..models import Canonical, GroupMember, NodeState, OwnerGroup, Peer, UserMfa
from ..settings import Settings

HIDDEN_KEY = "ui.setup_hidden_at"
BACKUP_FRESH = timedelta(days=7)
DEFAULT_PUBLIC_URL = "http://localhost:8000"


@dataclass(frozen=True)
class Item:
    key: str
    done: bool
    label: str
    why: str
    href: str | None


def _count(session: Session, model: Any, *where: Any) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def connected(session: Session) -> bool:
    """True once this node follows a peer or belongs to a node group."""
    return bool(_count(session, Peer) or _count(session, OwnerGroup) or _count(session, GroupMember))


def _backup_fresh(settings: Settings, now: datetime) -> bool:
    try:
        rows = backup.latest(settings)
    except OSError:
        return False
    return any(now - r["when"] <= BACKUP_FRESH for r in rows)


def items(session: Session, settings: Settings, *, user_id: str | None = None, providers: int = 0,
          now: datetime | None = None) -> list[Item]:
    now = now or datetime.now(UTC)
    factor = session.get(UserMfa, user_id) if user_id else None
    out = [
        Item("secret_key", bool(settings.secret_key), "Set a secret key",
             "Stored provider keys and sign-in secrets are encrypted with it. Set ATLAS_SECRET_KEY and restart.",
             None),
        Item("providers", providers > 0, "Turn on a metadata provider",
             "Providers fill in covers, dates and descriptions for new records.", "/admin/integrations"),
        Item("catalogue", _count(session, Canonical, Canonical.merged_into.is_(None)) > 0, "Add or import records",
             "Import a library export or add a first record by hand.", "/admin/import"),
        Item("backups", _backup_fresh(settings, now), "Make a backup",
             "Done when a backup is less than 7 days old.", "/admin/backup"),
        Item("mfa", bool(factor and factor.enabled_at), "Turn on two-factor sign-in for your account",
             "Administrator accounts can change everything on this node.", "/me/security"),
    ]
    if settings.role == "local":  # a public node is meant to take sign-ups; a personal one is not
        out.append(Item("registration", settings.registration == "closed" or bool(settings.initial_admin),
                        "Close registration once your accounts exist",
                        "On a personal node nobody else needs to sign up.", "/admin/settings#registration"))
    if connected(session):
        out.append(Item("public_url", settings.public_url.rstrip("/") != DEFAULT_PUBLIC_URL, "Set the public address",
                        "Other nodes reach this one at that address; localhost only works on this machine.",
                        "/admin/settings"))
    return out


def hidden(session: Session) -> bool:
    return session.get(NodeState, HIDDEN_KEY) is not None


def set_hidden(session: Session, value: bool, now: datetime | None = None) -> None:
    row = session.get(NodeState, HIDDEN_KEY)
    if value:
        stamp = (now or datetime.now(UTC)).isoformat()
        if row is None:
            session.add(NodeState(key=HIDDEN_KEY, value=stamp))
        else:
            row.value = stamp
    elif row is not None:
        session.delete(row)


def card(session: Session, settings: Settings, **kw: Any) -> dict[str, Any]:
    """Template context: the items, whether the card shows, and the optional section."""
    rows = items(session, settings, **kw)
    remaining = [i for i in rows if not i.done]
    is_hidden = hidden(session)
    return {"items": rows, "remaining": len(remaining), "total": len(rows), "hidden": is_hidden,
            "show": bool(remaining) and not is_hidden, "connected": connected(session)}
