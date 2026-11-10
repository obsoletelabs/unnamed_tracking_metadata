"""Portable backups: every table plus the cached media in one verifiable archive, restorable on SQLite or PostgreSQL.

An archive is a gzip'd tar holding `tables/<name>.jsonl`, `media/<blob>` and a `manifest.json` that lists the SHA-256 of
every member, the Atlas version and the database revision. It contains secrets (password hashes, tokens and, unless
ATLAS_SECRET_KEY is set, the node's signing key in plaintext), so it is written with owner-only permissions; encrypt it
before it leaves the machine. With ATLAS_SECRET_KEY the signing key and stored credentials are in it only encrypted
(`enc:v1:`), and the archive cannot be used without that secret (CONTRACTS-v1 C9.1).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from sqlalchemy import BigInteger, Date, DateTime, Engine, Integer, LargeBinary, Table, delete, func, select, text, update

from . import __version__, client_changes, db
from .models import Base
from .settings import Settings

log = logging.getLogger("atlas.backup")

FORMAT = 1
MANIFEST = "manifest.json"
BATCH = 500
_MEDIA_NAME = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{1,8}$")
_TABLE_NAME = re.compile(r"^tables/([a-z_0-9]+)\.jsonl$")


class BackupError(Exception):
    pass


def _tables() -> list[Table]:
    return list(Base.metadata.sorted_tables)  # parents before children


def _encode(table: Table, row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in table.columns:
        value = row._mapping[col]
        if isinstance(value, (datetime, date)):  # datetime is a date: both as ISO text
            value = value.isoformat()
        elif isinstance(value, bytes):
            value = base64.b64encode(value).decode()
        out[col.name] = value
    return out


def _decode(table: Table, data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in table.columns:
        if col.name not in data:
            raise BackupError(f"table {table.name} is missing column {col.name}; the archive is from a different schema")
        value = data[col.name]
        if value is not None and isinstance(col.type, DateTime):
            value = datetime.fromisoformat(value)
            if value.tzinfo is None and getattr(col.type, "timezone", False):
                value = value.replace(tzinfo=UTC)  # Atlas stores UTC; SQLite hands back naive values, PostgreSQL would guess
        elif value is not None and isinstance(col.type, Date):  # `canonical.release_start/_end` (0031)
            value = date.fromisoformat(value)
        elif value is not None and isinstance(col.type, LargeBinary):
            value = base64.b64decode(value)
        out[col.name] = value
    return out


def _revision(conn) -> str | None:
    try:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_files(settings: Settings) -> Iterator[Path]:
    root = Path(settings.media_dir)
    if root.is_dir():
        for p in sorted(root.glob("??/*")):
            if p.is_file() and _MEDIA_NAME.match(p.name):
                yield p


def create(settings: Settings, engine: Engine, out: Path, *, include_media: bool = True) -> dict[str, Any]:
    """Write a backup archive to `out` from one consistent snapshot of the database. Returns the manifest."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    files: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp_name, engine.connect() as conn:
        tmp = Path(tmp_name)
        if engine.dialect.name == "postgresql":
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif engine.dialect.name == "sqlite":
            db.begin_snapshot(conn)
        revision = _revision(conn)
        # Protect signing keys and account data while the archive is being written too.
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output, tarfile.open(fileobj=output, mode="w:gz") as tar:
            for table in _tables():
                path = tmp / f"{table.name}.jsonl"
                n = 0
                with path.open("w", encoding="utf-8") as f:
                    for row in conn.execute(select(table)).yield_per(BATCH):
                        f.write(json.dumps(_encode(table, row), separators=(",", ":"), ensure_ascii=False) + "\n")
                        n += 1
                name = f"tables/{table.name}.jsonl"
                files[name] = {"sha256": _sha_file(path), "rows": n}
                counts[table.name] = n
                tar.add(path, arcname=name, filter=_plain)
            conn.rollback()
            if include_media:
                for p in media_files(settings):
                    files[f"media/{p.name}"] = {"sha256": _sha_file(p)}
                    tar.add(p, arcname=f"media/{p.name}", filter=_plain)
            manifest = {"format": FORMAT, "atlas": __version__, "revision": revision, "database": engine.dialect.name,
                        "created_at": datetime.now(UTC).isoformat(), "tables": counts, "files": files,
                        "media_included": include_media}
            blob = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mode, info.mtime = len(blob), 0o600, int(datetime.now(UTC).timestamp())
            tar.addfile(info, io.BytesIO(blob))
    part.chmod(0o600)
    part.replace(out)
    return manifest


def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


def _members(path: Path) -> Iterator[tuple[tarfile.TarInfo, tarfile.TarFile]]:
    with tarfile.open(path, "r:gz") as tar:
        for m in tar:
            if not m.isreg() or m.name.startswith("/") or ".." in Path(m.name).parts:
                raise BackupError(f"unsafe entry in archive: {m.name!r}")
            yield m, tar


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        for m, tar in _members(path):
            if m.name == MANIFEST:
                data = json.load(tar.extractfile(m))  # type: ignore[arg-type]
                if data.get("format") != FORMAT:
                    raise BackupError(f"unsupported backup format {data.get('format')!r}")
                return data
    except (tarfile.TarError, OSError, EOFError, json.JSONDecodeError) as exc:
        raise BackupError(f"not a readable Atlas backup: {exc}") from exc
    raise BackupError("the archive has no manifest")


def verify(path: Path) -> dict[str, Any]:
    """Check every member against the manifest (and that nothing is missing or extra). Returns the manifest."""
    path = Path(path)
    manifest = read_manifest(path)
    want = dict(manifest["files"])
    try:
        for m, tar in _members(path):
            if m.name == MANIFEST:
                continue
            entry = want.pop(m.name, None)
            if entry is None:
                raise BackupError(f"unexpected member {m.name!r}")
            h = hashlib.sha256()
            f = tar.extractfile(m)
            while chunk := f.read(1 << 20):  # type: ignore[union-attr]
                h.update(chunk)
            if h.hexdigest() != entry["sha256"]:
                raise BackupError(f"checksum mismatch for {m.name}")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"the archive is damaged: {exc}") from exc
    if want:
        raise BackupError(f"the archive is incomplete; missing {sorted(want)[:3]}")
    return manifest


# table -> column holding a secrets_box token (the signing key and integration settings are handled apart)
_SECRET_COLUMNS = {"peer": "token_enc", "oidc_issuer": "client_secret_enc", "user_mfa": "secret_enc"}
_GROUP_SECRETS = {"owner_group": ("root_sealed", "box_sealed", "box_previous_sealed", "box_pending_sealed"),
                  "group_enrolment": ("secret_enc",)}


def _rows(path: Path, table: str) -> Iterator[dict[str, Any]]:
    with tarfile.open(path, "r:gz") as tar:
        try:
            member = tar.getmember(f"tables/{table}.jsonl")
        except KeyError:
            return
        for line in io.TextIOWrapper(tar.extractfile(member), encoding="utf-8"):  # type: ignore[arg-type]
            yield json.loads(line)


def secret_check(settings: Settings, path: Path) -> dict[str, Any]:
    """`backup verify --secret-check`: decrypt the archive's signing key and stored secrets with this node's
    ATLAS_SECRET_KEY (and ATLAS_SECRET_KEY_PREVIOUS), without restoring anything. Raises BackupError when
    there is no secret to check with or something does not decrypt."""
    from . import integrations, secrets_box, signing

    if not secrets_box.available(settings):
        raise BackupError("set ATLAS_SECRET_KEY to the secret of the node that made this backup to check it")
    path = Path(path)
    stored = next((r["value"] for r in _rows(path, "node_state") if r.get("key") == signing.SIGNING_KEY), None)
    result: dict[str, Any] = {"signing_key": None if stored is None else signing.at_rest(stored), "secrets": 0}
    if stored is not None:
        try:
            identity = signing.unseal(settings, stored)
        except (secrets_box.SecretsError, ValueError) as exc:
            raise BackupError(f"the signing key in this backup cannot be decrypted: {exc}") from None
        public = identity.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        result["signing_key_id"] = signing.key_id_for(public)
    tokens = [(table, row.get(column)) for table, column in _SECRET_COLUMNS.items() for row in _rows(path, table)]
    keys = {integrations.PREFIX + field for field in integrations.SECRET_FIELDS}
    tokens += [("setting", row.get("value")) for row in _rows(path, "setting") if row.get("key") in keys]
    for table, columns in _GROUP_SECRETS.items():  # owner-group secrets are stored as enc:v1:<token>
        for row in _rows(path, table):
            for column in columns:
                value = row.get(column)
                if isinstance(value, str) and value.startswith(signing.ENC_PREFIX):
                    tokens.append((table, value[len(signing.ENC_PREFIX):]))
    for table, token in tokens:
        if not isinstance(token, str) or not token:
            continue
        try:
            secrets_box.decrypt(settings, token)
        except secrets_box.SecretsError:
            raise BackupError(f"a stored secret in table {table} cannot be decrypted with ATLAS_SECRET_KEY") from None
        result["secrets"] += 1
    return result


def _require_readable_key(settings: Settings, path: Path) -> None:
    """Refuse, before anything is deleted, to restore an encrypted signing key this node cannot decrypt:
    the node would refuse to start afterwards, with the previous database already replaced."""
    from . import secrets_box, signing

    stored = next((r["value"] for r in _rows(path, "node_state") if r.get("key") == signing.SIGNING_KEY), None)
    if stored is None or signing.at_rest(stored) != "encrypted":
        return
    try:
        signing.unseal(settings, stored)
    except (secrets_box.SecretsError, ValueError):
        raise BackupError("the signing key in this backup is encrypted and ATLAS_SECRET_KEY (or "
                          "ATLAS_SECRET_KEY_PREVIOUS) on this node cannot decrypt it; set the secret of the node "
                          "that made the backup and run the restore again. Nothing was changed") from None


def _self_fk_columns(table: Table) -> list[str]:
    return [c.name for c in table.columns for fk in c.foreign_keys if fk.column.table is table]


def _has_rows(conn) -> list[str]:
    """Tables with data. The client change log epoch that migration 0023 writes into a freshly migrated
    database is not data, nor is the projection rebuild marker of migration 0038: the restore replaces both."""
    occupied = []
    for t in _tables():
        query = select(func.count()).select_from(t)
        if t.name == "node_state":
            query = query.where(t.c.key.not_in((client_changes.EPOCH, "scope_projection_pending")))
        if conn.execute(query).scalar():
            occupied.append(t.name)
    return occupied


def restore(settings: Settings, engine: Engine, path: Path, *, force: bool = False, include_media: bool = True,
            replay_journal: bool = True) -> dict[str, int]:
    """Load an archive into a database migrated to the archive's revision. Refuses a non-empty database unless forced."""
    path = Path(path)
    manifest = verify(path)
    _require_readable_key(settings, path)
    tables = {t.name: t for t in _tables()}
    with engine.begin() as conn:
        current = _revision(conn)
        if current != manifest["revision"]:
            raise BackupError(f"the database is at revision {current!r} but the backup is {manifest['revision']!r}; "
                              f"run `atlas migrate --revision {manifest['revision']}` first, restore, then `atlas migrate`")
        occupied = _has_rows(conn)
        if occupied and not force:
            raise BackupError(f"the database already has data ({', '.join(occupied[:4])}); use --force to replace it")
        for t in reversed(list(tables.values())):
            for col in _self_fk_columns(t):
                conn.execute(update(t).values({col: None}))
            conn.execute(delete(t))
        loaded: dict[str, int] = {}
        pending_fk: list[tuple[Table, dict[str, Any], dict[str, Any]]] = []
        by_table: dict[str, tarfile.TarInfo] = {}
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                hit = _TABLE_NAME.match(m.name)
                if hit:
                    by_table[hit.group(1)] = m
            for name in (t.name for t in _tables()):
                m = by_table.get(name)
                if m is None:
                    raise BackupError(f"the archive has no data for table {name}")
                table = tables[name]
                self_cols = _self_fk_columns(table)
                batch: list[dict[str, Any]] = []
                n = 0
                for line in io.TextIOWrapper(tar.extractfile(m), encoding="utf-8"):  # type: ignore[arg-type]
                    row = _decode(table, json.loads(line))
                    if any(row[col] is not None for col in self_cols):  # insert unlinked first, then link
                        pending_fk.append((table, {k: row[k] for k in table.primary_key.columns.keys()}, dict(row)))
                        row.update({col: None for col in self_cols})
                    batch.append(row)
                    n += 1
                    if len(batch) >= BATCH:
                        conn.execute(table.insert(), batch)
                        batch = []
                if batch:
                    conn.execute(table.insert(), batch)
                loaded[name] = n
            for table, pk, original in pending_fk:
                # every column is written back explicitly so onupdate defaults (updated_at) cannot fire
                values = {k: v for k, v in original.items() if k not in pk}
                conn.execute(update(table).where(*[table.c[k] == v for k, v in pk.items()]).values(values))
        _reset_sequences(conn, engine, list(tables.values()))
        if "client_change" in tables:
            # The restored client change log is older than (or foreign to) what clients have seen: a new epoch
            # makes every client cursor reset, even after the head passes it again (C3.2).
            node_state = tables["node_state"]
            conn.execute(delete(node_state).where(node_state.c.key == client_changes.EPOCH))
            conn.execute(node_state.insert().values(key=client_changes.EPOCH, value=client_changes.new_epoch()))
        if "owner_group" in tables:
            # A restored node may have missed revocations and successions: it pulls every member's state
            # before any privileged group action (CONTRACT-C10 §3.6).
            owner_group = tables["owner_group"]
            conn.execute(update(owner_group).where(owner_group.c.status == "active").values(status="resyncing"))
            in_group = conn.execute(select(func.count()).select_from(owner_group)).scalar_one() > 0
            if in_group and "node_state" in tables:
                # The private channel (§3.6): this node's next entries open a `restore` epoch, and it fetches a
                # snapshot of every linked account to re-learn effects the backup lost. A node in no group has no
                # stream to continue, so nothing is marked.
                node_state = tables["node_state"]
                # R1-3: the revocation journal (outside the database and the backups) is replayed before the
                # resync may finish, so revocations newer than the backup are back at once
                from . import group_journal

                marks = [("group_epoch_due", "restore"), ("group_snapshot_due", "1")]
                if group_journal.directory(settings) is not None:  # no journal: the resync pull alone protects
                    marks.append(("group_journal_replay_due", "1"))
                for key, value in marks:
                    conn.execute(delete(node_state).where(node_state.c.key == key))
                    conn.execute(node_state.insert().values(key=key, value=value))
    if include_media and manifest.get("media_included"):
        _restore_media(settings, path)
    if replay_journal:
        _replay_group_journal(settings, engine)
    return loaded


def _replay_group_journal(settings: Settings, engine: Engine) -> None:
    """Re-ingest the group revocation journal into the restored database, in its own transaction (C10 R1-3). A
    failure is reported, never fatal: the node stays `resyncing`, and startup and the next pull retry the replay."""
    from . import group_journal, groups
    from .db import make_sessionmaker
    from .signing import load_identity

    if group_journal.directory(settings) is None:
        return
    maker = make_sessionmaker(engine, settings)
    try:
        with maker() as session:
            group = groups.load(session)
            if group is None:
                return
            own = load_identity(session, settings).node_id
            group_journal.replay(session, group, own, settings=settings)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - the restore itself succeeded
        log.warning("group journal replay after restore failed: %s", exc)


def _reset_sequences(conn, engine: Engine, tables: list[Table]) -> None:
    if engine.dialect.name != "postgresql":
        return
    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) == 1 and isinstance(pk[0].type, (Integer, BigInteger)) and pk[0].autoincrement in (True, "auto") \
                and not pk[0].foreign_keys:
            col = pk[0].name
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{t.name}', '{col}'), COALESCE((SELECT MAX({col}) FROM {t.name}), 1), "  # noqa: E501
                f"(SELECT MAX({col}) IS NOT NULL FROM {t.name})) WHERE pg_get_serial_sequence('{t.name}', '{col}') IS NOT NULL"))  # noqa: E501


def _restore_media(settings: Settings, path: Path) -> None:
    root = Path(settings.media_dir)
    with tarfile.open(path, "r:gz") as tar:
        for m in tar:
            if not m.name.startswith("media/"):
                continue
            name = m.name.split("/", 1)[1]
            if not _MEDIA_NAME.match(name):
                raise BackupError(f"unsafe media name {name!r}")
            dest = root / name[:2] / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            with tmp.open("wb") as out:
                out.write(tar.extractfile(m).read())  # type: ignore[union-attr]
            tmp.replace(dest)


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete the oldest scheduled backups beyond `keep`."""
    files = sorted(Path(directory).glob("atlas-*.tar.gz"))
    doomed = files[:-keep] if keep > 0 else []
    for f in doomed:
        f.unlink(missing_ok=True)
    return doomed


def scheduled(settings: Settings, engine: Engine) -> Path:
    """Write one timestamped backup into settings.backup_dir and prune old ones."""
    assert settings.backup_dir
    out = Path(settings.backup_dir) / f"atlas-{datetime.now(UTC):%Y%m%d-%H%M%S}.tar.gz"
    create(settings, engine, out)
    prune(Path(settings.backup_dir), settings.backup_keep)
    return out


def latest(settings: Settings) -> list[dict[str, Any]]:
    if not settings.backup_dir:
        return []
    out = []
    for f in sorted(Path(settings.backup_dir).glob("atlas-*.tar.gz"), reverse=True)[:20]:
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "when": datetime.fromtimestamp(st.st_mtime, UTC)})
    return out



# ---- recovery evidence (#75) -------------------------------------------------------------------------------
# Three separate facts, never inferred from one another: an archive exists (`latest`), an archive was verified
# against its manifest (VERIFIED), and an archive was restored into an isolated database and its row counts
# matched (DRILL). The last two are stored in node_state so every worker and replica reports the same time.

VERIFIED = "backup:last_verified_at"
DRILL = "backup:last_restore_drill_at"


def record(engine: Engine, key: str, when: datetime | None = None) -> None:
    """Store when a verification or drill passed. Never raises: the evidence is optional, the backup is not."""
    from sqlalchemy.orm import Session

    from .models import NodeState

    try:
        with Session(engine) as s:
            db.sqlite_write_lock(s)  # a no-op except on SQLite
            row = s.get(NodeState, key)
            value = (when or datetime.now(UTC)).isoformat()
            if row is None:
                s.add(NodeState(key=key, value=value))
            else:
                row.value = value
            s.commit()
    except Exception:  # noqa: BLE001 - a busy or read-only database must not fail the backup itself
        pass


def recorded(session: Any, key: str) -> float | None:
    """Unix time of the stored evidence, or None when it was never recorded."""
    from .models import NodeState

    row = session.get(NodeState, key)
    if row is None:
        return None
    try:
        when = datetime.fromisoformat(row.value)
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


def drill(settings: Settings, path: Path, *, record_to: Engine | None = None) -> dict[str, Any]:
    """Restore `path` into a throwaway SQLite database and media directory (never the live ones), and compare the
    rows loaded with the manifest. On success the drill (and the verification it includes) is recorded in
    `record_to` when given. Raises BackupError on any failure."""
    path = Path(path)
    manifest = verify(path)
    with tempfile.TemporaryDirectory(prefix="atlas-drill-") as tmp:
        url = "sqlite:///" + (Path(tmp) / "drill.db").as_posix()
        isolated = settings.model_copy(update={"database_url": url, "media_dir": str(Path(tmp) / "media"),
                                                "backup_dir": None})
        try:
            db.upgrade(url, manifest["revision"] or "head", settings=isolated)
        except Exception as exc:  # noqa: BLE001 - an archive from a revision this release cannot build
            raise BackupError(f"cannot prepare a database at revision {manifest['revision']!r}: "
                              f"{type(exc).__name__}") from exc
        engine = db.make_engine(url)
        try:
            loaded = restore(isolated, engine, path, include_media=bool(manifest.get("media_included")),
                             replay_journal=False)
        finally:
            engine.dispose()
    expected = manifest.get("tables", {})
    wrong = sorted(t for t, n in expected.items() if loaded.get(t, 0) != n)
    if wrong:
        raise BackupError(f"restored row counts differ from the manifest for {', '.join(wrong[:4])}")
    if record_to is not None:
        now = datetime.now(UTC)
        record(record_to, VERIFIED, now)
        record(record_to, DRILL, now)
    return {"rows": sum(loaded.values()), "tables": len(loaded), "revision": manifest["revision"],
            "created_at": manifest["created_at"]}
