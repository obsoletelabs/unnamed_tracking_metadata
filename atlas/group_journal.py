"""The revocation journal of an owner group (CONTRACT-C10 §C10.17 R1-3).

Every accepted CRL, succession and cancel is appended, as the signed object, to `<group_journal_dir>/<group_id>.jsonl`
(one JSON line each, fsync after every write). The directory lies outside the database, `media_dir` and
`backup_dir`, so a restore cannot roll it back: after a restore, and at startup when the database lacks any of its
objects, the journal is ingested again before anything else. The objects verify themselves (a tampered line is
simply refused by the normal ingest checks), and the file holds nothing secret: only what `state` already serves.

Succession lines carry this node's local `received_at`; an `activation` line records when this node activated one,
so a replayed succession keeps its activation state instead of waiting out another delay.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger("atlas.groups")

KINDS = ("crl", "succession", "cancel", "activation")
REPLAY_DUE = "group_journal_replay_due"  # NodeState, set by a restore: replay before the resync may finish
REPLAYED = "group_journal_replayed"  # NodeState: when the journal was last replayed
_LOCK = threading.Lock()
# Windows byte-range locks are mandatory: the lock byte lies far past any real journal, so readers are never blocked
_LOCK_AT = 2 ** 31 - 2
_SEEN: dict[str, tuple[tuple[int, float], set[str]]] = {}  # path -> ((size, mtime), line keys)


class JournalConfigError(ValueError):
    """The journal directory is inside the media or backup directory (it would be rolled back with them)."""


def directory(settings: Any) -> Path | None:
    raw = getattr(settings, "group_journal_dir", None) if settings is not None else None
    return Path(raw) if raw else None


def check_location(settings: Any) -> None:
    """Refuse a journal directory inside `media_dir` or `backup_dir` (both are archived or restored together with
    the database, which would defeat the journal)."""
    journal = directory(settings)
    if journal is None:
        return
    target = journal.resolve()
    for name in ("media_dir", "backup_dir"):
        other = getattr(settings, name, None)
        if not other:
            continue
        base = Path(other).resolve()
        if target == base or base in target.parents:
            raise JournalConfigError(f"ATLAS_GROUP_JOURNAL_DIR ({journal}) must not be inside {name} ({other}): "
                                     "the revocation journal has to survive a restore of the database and media")


def path_for(settings: Any, group_id: str) -> Path | None:
    journal = directory(settings)
    return journal / f"{group_id}.jsonl" if journal is not None else None


@contextmanager
def _file_lock(handle) -> Iterator[None]:
    """A per-process lock plus an OS lock, so several workers on one host append whole lines."""
    with _LOCK:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(_LOCK_AT)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass  # a file system without locks: the per-process lock still holds
        try:
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(_LOCK_AT)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _line_key(line: dict) -> str:
    obj = line.get("obj") if isinstance(line, dict) else None
    digest = obj.get("body_hash") if isinstance(obj, dict) else None
    return f"{line.get('kind')}:{digest}"


def read(settings: Any, group_id: str) -> list[dict]:
    """Every well-formed line of the group's journal (a torn or garbled line is skipped)."""
    path = path_for(settings, group_id)
    if path is None or not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("kind") in KINDS and isinstance(line.get("obj"), dict):
                out.append(line)
    return out


def _known(path: Path) -> set[str]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return set()
    stamp = (st.st_size, st.st_mtime)
    cached = _SEEN.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    keys = set()
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                keys.add(_line_key(json.loads(raw)))
            except ValueError:
                continue
    _SEEN[str(path)] = (stamp, keys)
    return keys


def append(settings: Any, group_id: str, kind: str, obj: dict, **extra: Any) -> bool:
    """Append one signed object (once per kind and body hash). Returns whether a line was written."""
    path = path_for(settings, group_id)
    if path is None or kind not in KINDS:
        return False
    line = {"kind": kind, "obj": obj, **{k: v for k, v in extra.items() if v is not None}}
    key = _line_key(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle, _file_lock(handle):
        if key in _known(path):
            return False
        handle.seek(0, os.SEEK_END)
        handle.write((json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    _SEEN.pop(str(path), None)
    return True


def record(session: Session, group_id: str, kind: str, obj: dict, **extra: Any) -> None:
    """Append from code that holds only a session: the settings travel in `session.info` (db.make_sessionmaker).
    A failing write is logged, never fatal: the database stays the primary store."""
    if session.info.get("atlas_journal_replaying"):
        return
    settings = session.info.get("atlas_settings")
    try:
        append(settings, group_id, kind, obj, **extra)
    except OSError as exc:
        log.warning("group journal write failed: %s", exc,
                    extra={"atlas_event": {"event": "group.journal_failed", "error_type": type(exc).__name__}})


def missing(session: Session, group: Any, lines: list[dict]) -> list[dict]:
    """The journal lines whose object this database does not hold."""
    from . import groups
    from .models import GroupCrl, NodeState

    have = {s["body_hash"] for s in group.succession or []}
    pending = {p["body"]["body_hash"] for p in group.succession_pending or []}
    cancels = {c["body_hash"] for c in group.succession_cancels or []}
    out = []
    for line in lines:
        obj, kind = line["obj"], line["kind"]
        digest = obj.get("body_hash")
        if kind == "crl":
            body = obj.get("body") if isinstance(obj.get("body"), dict) else {}
            stored = session.get(GroupCrl, (group.group_id, body.get("issuer"), body.get("crl_seq"), digest)) \
                if isinstance(body.get("issuer"), str) and isinstance(body.get("crl_seq"), int) else None
            held = session.get(NodeState, groups.CRL_HELD_PREFIX + str(digest)[:40])
            if stored is None and held is None:
                out.append(line)
        elif kind == "succession" and digest not in have and digest not in pending:
            out.append(line)
        elif kind == "activation" and digest not in have:
            out.append(line)
        elif kind == "cancel" and digest not in cancels:
            out.append(line)
    return out


def replay(session: Session, group: Any, own_node: str | None, now: datetime | None = None,
           settings: Any = None) -> dict[str, int]:
    """Ingest every journal object again (successions with their recorded times, then cancels, then CRLs) and
    rebuild. Objects verify themselves; whatever the database already holds is 'known'."""
    from . import group_crypto as gc
    from . import groups
    from .models import NodeState

    settings = settings or session.info.get("atlas_settings")
    now = now or groups.utcnow()
    lines = read(settings, group.group_id)
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    activated = {line["obj"].get("body_hash"): line.get("activated_at") for line in lines
                 if line["kind"] == "activation"}
    session.info["atlas_journal_replaying"] = True
    try:
        succs = sorted((ln for ln in lines if ln["kind"] == "succession"),
                       key=lambda ln: (ln["obj"].get("body", {}).get("index", 0) if isinstance(
                           ln["obj"].get("body"), dict) else 0))
        for line in succs:
            obj = line["obj"]
            try:
                received = gc.parse_time(line.get("received_at")) if line.get("received_at") else now
            except gc.GroupCryptoError:
                received = now
            outcome = groups.ingest_succession(session, group, obj, received)
            bump(f"succession:{outcome}")
            when = activated.get(obj.get("body_hash"))
            if outcome in ("pending", "known") and when:
                # this node had activated it before the restore: it stays active (no second delay, no second
                # confirmation)
                index = obj["body"]["index"]
                session.merge(NodeState(key=groups.SUCCESSION_CONFIRM_PREFIX + str(index), value="journal"))
                session.flush()
                try:
                    at = max(gc.parse_time(when), received)
                except gc.GroupCryptoError:
                    at = now
                due = [p for p in groups.pending_successions(group) if p["body"]["body"]["index"] == index]
                if due:
                    groups.activate_successions(session, group, max(at, groups.t(due[0]["activates_at"])), own_node)
        for line in lines:
            if line["kind"] == "cancel":
                bump("cancel:" + groups.ingest_cancel(session, group, line["obj"], now))
        for line in lines:
            if line["kind"] == "crl":
                bump("crl:" + groups.ingest_crl(session, group, line["obj"], now))
    finally:
        session.info.pop("atlas_journal_replaying", None)
    if own_node:
        groups.rebuild(session, group, own_node, now, settings)
    due = session.get(NodeState, REPLAY_DUE)
    if due is not None:
        session.delete(due)
    session.merge(NodeState(key=REPLAYED, value=gc.stamp(now)))
    groups.audit(session, None, "group_journal_replayed", group.group_id, {"lines": len(lines), "outcomes": counts})
    session.flush()
    return counts


def replay_if_needed(session: Session, group: Any, own_node: str | None, settings: Any = None) -> dict[str, int] | None:
    """At startup (and before a resync completes): replay when a restore asked for it or when the journal holds an
    object the database lacks."""
    from .models import NodeState

    settings = settings or session.info.get("atlas_settings")
    if directory(settings) is None:
        return None
    lines = read(settings, group.group_id)
    if session.get(NodeState, REPLAY_DUE) is None and not missing(session, group, lines):
        return None
    return replay(session, group, own_node, settings=settings)


def startup_replay(maker: Any, own_node: str) -> None:
    from . import groups

    with maker() as session:
        group = groups.load(session)
        if group is None or group.status in ("left", "revoked"):
            return
        if replay_if_needed(session, group, own_node) is not None:
            session.commit()
