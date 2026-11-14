"""federation ergonomics: introduced records, waiting entries, the followed feed, media sources (C13, A19)

Revision ID: 0036
Revises: 0035

Upgrade (CONTRACT-C13 §9.3):
* New table `feed_introduced`: which records this node's own signed feed introduced. Backfilled from `change_log`:
  the `canonical_id` of every `obs` (`via = obs`) and the `new_canonical_id` of every `detach` (`via = detach`).
  A record that a signed sidecar, lock, merge or relation names but that nothing introduced is marked
  `via = legacy` (`legacy_unintroduced`, `seq` NULL): `atlas feed introduce --missing` publishes it once the emit
  protocol reaches 1.5 or the bridge is on. A node that signs nothing (mirror, local) gets an empty table.
* `peer.follow` and `peer.via_node_id` (NULL: a peer added before A19, whose mode stays inferred until the operator
  confirms it). A choice kept aside by a downgrade (`node_state["peer_0036_kept"]`) is restored.
* `sync_failure.waiting_for` and the index `ix_sync_failure_waiting`. Open quarantined `apply_error` rows of the
  kinds that name records, whose error says the record was unknown ("unknown canonical", "unknown or merged
  canonical", "merge of an unknown record", "unknown record ..."), become `wait` / `waiting_for_record`, waiting
  for the first named record this node does not hold. A row whose records are all here now (one was merged
  since) stays `quarantined`: its next automatic retry applies it to the survivor (C13 §5.2 redirects; a `merge`
  or `detach` keeps its own rules).
* `artwork` gains `origin_node`, `fetched_from`, `sources_tried`, `peer_digest` and `digest_provenance`.
  `origin_node` is backfilled from the retained `artwork` entries: a row a retained entry lists (same record,
  source and address) came from that entry's feed.

Downgrade (needs no flag; the previous release reads every remaining row correctly):
* `wait` rows become `quarantined` / `apply_error` with `attempts = 0` and an "unknown canonical" message, so the
  previous release retries them exactly as it retried them before; upgrading again turns them back into `wait`.
* `peer.follow` / `peer.via_node_id` of every peer that has a choice are kept aside as JSON in the existing
  `node_state` table (key `peer_0036_kept`, which the previous release never reads), so the schema at 0035 is
  exactly the previous release's.
* `feed_introduced` is dropped (upgrading again rebuilds it from the feed itself), as are the media columns (they
  describe the cache, which the previous release keeps using as before).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None

PEER_KEPT = "peer_0036_kept"
BATCH = 1000
#: Kinds whose entries name records, and the body fields that do (in the order `sync` checks them).
NAMES = {"artwork": ("canonical_id",), "achievements": ("canonical_id",), "custom": ("canonical_id",),
         "lock": ("canonical_id",), "unlock": ("canonical_id",), "merge": ("src", "dst"),
         "relation": ("from", "to"), "detach": ("canonical_id",)}
#: What the previous releases' apply said when the record was not there.
UNKNOWN = ("unknown canonical", "unknown or merged canonical", "merge of an unknown record", "unknown record")
ART_COLUMNS = (("origin_node", sa.String(64)), ("fetched_from", sa.String(80)), ("sources_tried", sa.JSON()),
               ("peer_digest", sa.String(64)), ("digest_provenance", sa.String(16)))
DOWN_MESSAGE = "AtlasError: unknown canonical (waited for its record under a newer release)"


def _body(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _cid(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("atlas:") and len(value) <= 40 else None


def _namespace(source: str) -> str:
    """`atlas.normalize.norm_namespace`, copied: a migration does not import application code."""
    return re.sub(r"[^a-z0-9._-]", "-", source.strip().casefold())[:50] or "unknown"


def _names(kind: str, body: dict[str, Any]) -> list[str]:
    return [c for c in (_cid(body.get(f)) for f in NAMES.get(kind, ())) if c]


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "feed_introduced",
        sa.Column("canonical_id", sa.String(40), primary_key=True),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("via", sa.String(8), nullable=False),
        sa.Column("because", sa.String(16), nullable=True),
        sa.Column("introduced_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("peer") as batch:
        batch.add_column(sa.Column("follow", sa.String(8), nullable=True))
        batch.add_column(sa.Column("via_node_id", sa.String(64), nullable=True))
    with op.batch_alter_table("sync_failure") as batch:
        batch.add_column(sa.Column("waiting_for", sa.String(40), nullable=True))
    op.create_index("ix_sync_failure_waiting", "sync_failure", ["waiting_for", "resolved_at"])
    with op.batch_alter_table("artwork") as batch:
        for name, kind in ART_COLUMNS:
            batch.add_column(sa.Column(name, kind, nullable=True))

    _restore_peers(bind)
    _backfill_introduced(bind)
    _waiting_rows(bind)
    _artwork_origins(bind)


def _restore_peers(bind: Any) -> None:
    raw = bind.execute(sa.text("SELECT value FROM node_state WHERE key = :k"), {"k": PEER_KEPT}).scalar()
    if raw is None:
        return
    try:
        kept = json.loads(raw)
    except ValueError:
        kept = []
    for item in kept if isinstance(kept, list) else []:
        if isinstance(item, dict) and item.get("follow") in ("own", "relayed"):
            bind.execute(sa.text("UPDATE peer SET follow = :f, via_node_id = :v WHERE id = :i"),
                         {"f": item["follow"], "v": item.get("via_node_id"), "i": str(item.get("id"))})
    bind.execute(sa.text("DELETE FROM node_state WHERE key = :k"), {"k": PEER_KEPT})


def _backfill_introduced(bind: Any) -> None:
    now = datetime.now(UTC)
    introduced: dict[str, tuple[int | None, str]] = {}
    named: set[str] = set()
    last = 0
    while True:
        rows = bind.execute(sa.text("SELECT seq, kind, body FROM change_log WHERE seq > :s ORDER BY seq LIMIT :n"),
                            {"s": last, "n": BATCH}).all()
        if not rows:
            break
        for seq, kind, raw in rows:
            last = seq
            body = _body(raw)
            if kind == "obs" and (cid := _cid(body.get("canonical_id"))):
                introduced.setdefault(cid, (seq, "obs"))
            elif kind == "record" and (cid := _cid(body.get("canonical_id"))):
                introduced.setdefault(cid, (seq, "record"))
            elif kind == "detach":
                if cid := _cid(body.get("new_canonical_id")):
                    introduced.setdefault(cid, (seq, "detach"))
            named.update(_names(kind, body))
    live = {cid for (cid,) in bind.execute(sa.text("SELECT id FROM canonical WHERE merged_into IS NULL"))}
    table = sa.table("feed_introduced", sa.column("canonical_id"), sa.column("seq"), sa.column("via"),
                     sa.column("because"), sa.column("introduced_at"))
    values = [{"canonical_id": cid, "seq": seq, "via": via, "because": None, "introduced_at": now}
              for cid, (seq, via) in introduced.items()]
    values += [{"canonical_id": cid, "seq": None, "via": "legacy", "because": None, "introduced_at": now}
               for cid in sorted(named - set(introduced)) if cid in live]
    for chunk in range(0, len(values), BATCH):
        bind.execute(table.insert(), values[chunk:chunk + BATCH])


def _waiting_rows(bind: Any) -> None:
    rows = bind.execute(sa.text(
        "SELECT f.node_id, f.seq, f.kind, f.error, r.body FROM sync_failure f JOIN replicated_change r "
        "ON r.node_id = f.node_id AND r.seq = f.seq WHERE f.resolved_at IS NULL AND f.disposition = 'quarantined' "
        "AND f.reason = 'apply_error'")).all()
    for node_id, seq, kind, error, raw in rows:
        if kind not in NAMES or not any(text in (error or "") for text in UNKNOWN):
            continue
        names = _names(kind, _body(raw))
        if not names:
            continue
        missing = [c for c in names
                   if bind.execute(sa.text("SELECT 1 FROM canonical WHERE id = :c"), {"c": c}).first() is None]
        if not missing:
            continue  # merged since: the retry applies it through the redirect
        bind.execute(sa.text(
            "UPDATE sync_failure SET disposition = 'wait', reason = 'waiting_for_record', waiting_for = :w "
            "WHERE node_id = :n AND seq = :s"), {"w": missing[0], "n": node_id, "s": seq})


def _artwork_origins(bind: Any) -> None:
    last: tuple[str, int] = ("", 0)
    while True:
        rows = bind.execute(sa.text(
            "SELECT node_id, seq, body FROM replicated_change WHERE kind = 'artwork' AND "
            "(node_id > :n OR (node_id = :n AND seq > :s)) ORDER BY node_id, seq LIMIT :b"),
            {"n": last[0], "s": last[1], "b": BATCH}).all()
        if not rows:
            break
        for node_id, seq, raw in rows:
            last = (node_id, seq)
            body = _body(raw)
            cid, source = _cid(body.get("canonical_id")), body.get("source")
            items = body.get("items") if isinstance(body.get("items"), list) else []
            urls = sorted({i["url"] for i in items if isinstance(i, dict) and isinstance(i.get("url"), str)})
            if not cid or not isinstance(source, str) or not urls:
                continue
            for url in urls[:200]:
                bind.execute(sa.text(
                    "UPDATE artwork SET origin_node = :n WHERE canonical_id = :c AND source = :src AND url = :u"),
                    {"n": node_id[:64], "c": cid, "src": _namespace(source), "u": url})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE sync_failure SET disposition = 'quarantined', reason = 'apply_error', attempts = 0, error = :m "
        "WHERE disposition = 'wait'"), {"m": DOWN_MESSAGE})
    kept = bind.execute(sa.text("SELECT id, follow, via_node_id FROM peer WHERE follow IS NOT NULL")).all()
    bind.execute(sa.text("DELETE FROM node_state WHERE key = :k"), {"k": PEER_KEPT})
    if kept:
        bind.execute(sa.text("INSERT INTO node_state (key, value) VALUES (:k, :v)"), {"k": PEER_KEPT, "v": json.dumps(
            [{"id": i, "follow": f, "via_node_id": v} for i, f, v in kept], sort_keys=True)})
    with op.batch_alter_table("artwork") as batch:
        for name, _kind in ART_COLUMNS:
            batch.drop_column(name)
    op.drop_index("ix_sync_failure_waiting", table_name="sync_failure")
    with op.batch_alter_table("sync_failure") as batch:
        batch.drop_column("waiting_for")
    with op.batch_alter_table("peer") as batch:
        batch.drop_column("via_node_id")
        batch.drop_column("follow")
    op.drop_table("feed_introduced")
