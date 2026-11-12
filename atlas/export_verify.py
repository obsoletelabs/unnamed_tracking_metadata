"""Pre-upgrade export and verify of a node's data (C16 R2, the "v0 upgrades in place with no data loss" gate).

`atlas export-verify export MANIFEST` reads every table of the configured database, offline and read-only, and
writes a manifest: the Alembic revision, and per table its columns, row count, one content hash per row (over the
exported columns only, in primary-key order) and a hash of the whole table. Run it on the v0 node before the upgrade.

`atlas export-verify verify MANIFEST` reads the database again (after the upgrade) and checks that every row the
manifest lists is still there with the same content in those columns: nothing lost, nothing changed. An upgrade may
add tables, columns and rows (wire 1 adds `follower_wire`, `peer.remote_wire`, `node_state["wire_high_water"]`...),
so those are reported, never failures. Widened columns (D5: 16 -> 64 characters) hold the same values and hash alike.

The manifest holds hashes only, never row content: it can be kept beside the backup without exposing the data.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, select
from sqlalchemy.engine import Engine

FORMAT = "atlas-export-verify"
VERSION = 1
#: Bookkeeping tables an upgrade rewrites on purpose; recorded, never compared.
SKIP = frozenset({"alembic_version"})
#: Rows a migration rewrites on purpose, by table: (key column, {key: why}). They are exported by key, not by content,
#: and verify checks that each is still there (reported as `transformed`, never as lost).
TRANSFORMED: dict[str, tuple[str, dict[str, str]]] = {
    "node_state": ("key", {"signing_key": "migration 0024 encrypts the node signing key at rest"}),
}
BATCH = 2000


def _value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"b": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, datetime):
        value = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def row_hash(columns: list[str], row: Any) -> str:
    data = [[c, _value(row[c])] for c in columns]
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _table_rows(engine: Engine, table: Any, columns: list[str]) -> tuple[list[str], list[str]]:
    """Per-row hashes in primary-key order, and the keys of the rows `TRANSFORMED` names (left out of the hashes)."""
    order = list(table.primary_key.columns) or [table.c[c] for c in columns]
    key_column, keys = TRANSFORMED.get(table.name, (None, {}))
    out: list[str] = []
    transformed: list[str] = []
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(
            select(*[table.c[c] for c in columns]).order_by(*order))
        for chunk in result.mappings().partitions(BATCH):
            for r in chunk:
                if key_column is not None and r.get(key_column) in keys:
                    transformed.append(r[key_column])
                else:
                    out.append(row_hash(columns, r))
    return out, transformed


def _revision(engine: Engine, meta: MetaData) -> str | None:
    if "alembic_version" not in meta.tables:
        return None
    with engine.connect() as conn:
        rows = [r[0] for r in conn.execute(select(meta.tables["alembic_version"].c.version_num))]
    return ",".join(sorted(rows)) or None


def export(engine: Engine) -> dict[str, Any]:
    """The manifest of the database behind `engine` (read-only)."""
    meta = MetaData()
    meta.reflect(bind=engine)
    tables: dict[str, Any] = {}
    for name in sorted(meta.tables):
        if name in SKIP:
            continue
        table = meta.tables[name]
        columns = sorted(c.name for c in table.columns)
        rows, transformed = _table_rows(engine, table, columns)
        tables[name] = {"columns": columns, "rows": len(rows) + len(transformed), "row_hashes": rows,
                        "transformed": transformed,
                        "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
    return {"format": FORMAT, "v": VERSION, "revision": _revision(engine, meta), "dialect": engine.dialect.name,
            "created_at": datetime.now(UTC).isoformat(), "tables": tables}


def verify(engine: Engine, manifest: dict[str, Any]) -> dict[str, Any]:
    """Compare the database with a manifest. `ok` is False when any listed row is missing or changed."""
    if manifest.get("format") != FORMAT or manifest.get("v") != VERSION:
        raise ValueError("not an atlas export-verify manifest of a version this release reads")
    meta = MetaData()
    meta.reflect(bind=engine)
    problems: list[str] = []
    report: dict[str, Any] = {}
    for name, want in sorted(manifest["tables"].items()):
        if name not in meta.tables:
            problems.append(f"table {name} is gone ({want['rows']} rows)")
            report[name] = {"missing_table": True}
            continue
        table = meta.tables[name]
        have_columns = {c.name for c in table.columns}
        lost_columns = [c for c in want["columns"] if c not in have_columns]
        if lost_columns:
            problems.append(f"table {name} lost columns {', '.join(lost_columns)}")
            report[name] = {"missing_columns": lost_columns}
            continue
        rows, transformed = _table_rows(engine, table, list(want["columns"]))
        present = set(rows)
        missing = sum(1 for h in want["row_hashes"] if h not in present)
        gone = [k for k in want.get("transformed", []) if k not in set(transformed)]
        if missing:
            problems.append(f"table {name}: {missing} of {want['rows']} rows missing or changed")
        if gone:
            problems.append(f"table {name}: rewritten rows {', '.join(gone)} are gone")
        added_columns = sorted(have_columns - set(want["columns"]))
        why = TRANSFORMED.get(name, (None, {}))[1]
        report[name] = {"rows_before": want["rows"], "rows_after": len(rows) + len(transformed),
                        "missing_or_changed": missing + len(gone), "added_columns": added_columns,
                        "transformed": {k: why.get(k, "") for k in want.get("transformed", []) if k not in gone},
                        "identical": hashlib.sha256("\n".join(rows).encode()).hexdigest() == want["sha256"]}
    added_tables = sorted(t for t in meta.tables if t not in manifest["tables"] and t not in SKIP)
    return {"ok": not problems, "problems": problems, "revision_before": manifest.get("revision"),
            "revision_after": _revision(engine, meta), "added_tables": added_tables, "tables": report}


def write(path: str | Path, manifest: dict[str, Any]) -> None:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    Path(path).write_bytes(gzip.compress(raw, mtime=0) if str(path).endswith(".gz") else raw)


def read(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)
