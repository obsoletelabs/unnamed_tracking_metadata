"""matching, provenance and reversible merges (CONTRACT-C11 §11.13: the 0031 and 0032 schema of the contract, as one
revision; numbers 0031-0033 were taken by A14-A16, amendment A17 #1)

Revision ID: 0034
Revises: 0033

New tables (all local to this node, never federated):

* `match_decision` (§11.4): one row per ingested observation that was not `unchanged`: tier, reason, score,
  margin, evidence items and the candidates considered.
* `match_rejection` (§11.6): remembered "different works" (`record`) and "this source record is not that work"
  (`chain`) decisions, with a partial unique index on the active ones.
* `record_live` (§11.6): merged record id -> live record, indexed on the live id.
* `field_conflict` (§11.7.2): which fields of which records have conflicting provider values.
* `review_candidate` (§11.9.2): the candidates of each review item as rows, so a merge finds the items it touches
  with an index instead of reading the whole queue (derived from `review_item.candidates`).
* `merge_record`, `merge_record_item` (§11.10): every merge from now on, with the snapshot that undoes it.

New columns: `review_item.reason, evidence, decision_id, pair_key, priority, resolved_at` (§11.5, with the
`ix_review_queue` index and the partial unique `uq_review_open_pair`), `observation.payload_hash, last_seen_at`
(§11.9.1), `crosswalk.variant` (§11.2.4), `field_lock.cleared` (§11.8), plus `merge_record.feed_seq` (amendment
A17 #4).

Existing data: no record, view, version or feed entry changes. `record_live` is filled from `canonical.merged_into`
(merge chains followed, at most 20 hops). Open review items get their `pair_key` and `priority`; when two open
items describe the same pair, the newer one becomes `superseded`. `payload_hash` stays NULL (computed lazily),
`crosswalk.variant` stays '' (every existing key is a work key, §11.2.4 rule 4). Merges made before this revision
have no record (`legacy_merge`): only per-observation detach undoes them. `explain` v2 is written by the next
rebuild of each record, or for every record by `atlas match explain-backfill`.

Downgrade keeps what the previous release cannot read aside instead of dropping it (the A15/A16 pattern, amendment
A17 #2): the new tables are renamed to `*_0034_kept` (the derived `field_conflict` and `review_candidate` are
dropped), the new review columns go to `review_item_0034_kept` (open items of kinds the previous release does not
know, `title_contradicted` and `unmerge_check`, are set aside there and hidden from its queue as `superseded`),
`observation.last_seen_at` goes to `observation_0034_kept`, explicit-clear
locks move to `field_lock_0034_cleared_kept` (the previous release would read their NULL value as a pin), and
variant crosswalk keys are listed in `crosswalk_variant_0034_kept`. The `_meta` key is removed from `explain` (the
previous release's game page lists every explain key). Upgrading again brings everything back: tables renamed back,
`record_live` recomputed (the previous release may have merged records meanwhile; those merges stay legacy), cleared
locks restored on their live record unless that field was locked meanwhile, variants restored on keys that still
exist, review columns and statuses restored on items that still exist (a pair the previous release reopened keeps
one open item), `last_seen_at` restored. Only `observation.payload_hash` is not kept: it is recomputed lazily.
The derived `explain._meta` and `field_conflict` rows are re-derived by the application after the re-upgrade: the
downgrade leaves `node_state["view_rebuild_pending"]` (`atlas.dates_rebuild.finish_pending`, Phase 4 defect D3).
"""
from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

KEPT_TABLES = ("match_decision", "match_rejection", "merge_record", "merge_record_item")
CLEARED_KEPT = "field_lock_0034_cleared_kept"
VARIANT_KEPT = "crosswalk_variant_0034_kept"
REVIEW_KEPT = "review_item_0034_kept"
SEEN_KEPT = "observation_0034_kept"
#: Review item kinds the previous release (0033) knows; open items of other kinds are hidden from it.
OLD_KINDS = ("possible_duplicate", "id_conflict")
MAX_HOPS = 20
BATCH = 1000
#: §11.5 rule 5 (the score part of `possible_duplicate` is added per item).
PRIORITY = {"id_conflict": 30, "bridge_deferred": 25, "title_contradicted": 20, "unmerge_check": 15,
            "possible_duplicate": 10}

canonical = sa.table("canonical", sa.column("id", sa.String), sa.column("merged_into", sa.String),
                     sa.column("explain", sa.JSON))
record_live = sa.table("record_live", sa.column("id", sa.String), sa.column("live_id", sa.String))
review = sa.table("review_item", sa.column("id", sa.String), sa.column("kind", sa.String),
                  sa.column("canonical_id", sa.String), sa.column("candidates", sa.JSON),
                  sa.column("confidence", sa.Float), sa.column("status", sa.String),
                  sa.column("created_at", sa.DateTime), sa.column("pair_key", sa.String),
                  sa.column("priority", sa.Integer), sa.column("reason", sa.String), sa.column("evidence", sa.JSON),
                  sa.column("decision_id", sa.String), sa.column("resolved_at", sa.DateTime(timezone=True)))
review_candidate = sa.table("review_candidate", sa.column("review_id", sa.String),
                            sa.column("canonical_id", sa.String))
observation = sa.table("observation", sa.column("id", sa.String),
                       sa.column("last_seen_at", sa.DateTime(timezone=True)))
lock = sa.table("field_lock", sa.column("canonical_id", sa.String), sa.column("field", sa.String),
                sa.column("value", sa.JSON), sa.column("set_by", sa.String), sa.column("local_only", sa.Boolean),
                sa.column("created_at", sa.DateTime), sa.column("cleared", sa.Boolean))
crosswalk = sa.table("crosswalk", sa.column("namespace", sa.String), sa.column("external_id", sa.String),
                     sa.column("variant", sa.String))


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    restoring = all(f"{t}_0034_kept" in tables for t in KEPT_TABLES)
    if restoring:
        for name in KEPT_TABLES:
            op.rename_table(f"{name}_0034_kept", name)
    else:
        _create_tables()
    op.create_table(
        "record_live",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("live_id", sa.String(40), nullable=False),
    )
    op.create_index("ix_record_live_live_id", "record_live", ["live_id"])
    op.create_table(
        "field_conflict",
        sa.Column("canonical_id", sa.String(40), primary_key=True),
        sa.Column("field", sa.String(48), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_field_conflict_field", "field_conflict", ["field", "canonical_id"])
    op.create_table(
        "review_candidate",
        sa.Column("review_id", sa.String(26), primary_key=True),
        sa.Column("canonical_id", sa.String(40), primary_key=True),
    )
    op.create_index("ix_review_candidate_cid", "review_candidate", ["canonical_id", "review_id"])

    with op.batch_alter_table("review_item") as batch:
        batch.add_column(sa.Column("reason", sa.String(32), nullable=True))
        batch.add_column(sa.Column("evidence", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("decision_id", sa.String(26), nullable=True))
        batch.add_column(sa.Column("pair_key", sa.String(32), nullable=True))
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("observation") as batch:
        batch.add_column(sa.Column("payload_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("crosswalk") as batch:
        batch.add_column(sa.Column("variant", sa.String(120), nullable=False, server_default=""))
    with op.batch_alter_table("field_lock") as batch:
        batch.add_column(sa.Column("cleared", sa.Boolean(), nullable=False, server_default=sa.false()))

    _backfill_record_live(bind)
    if REVIEW_KEPT in tables:
        _restore_reviews(bind)
    if SEEN_KEPT in tables:
        _restore_seen(bind)
    _backfill_reviews(bind)
    _backfill_review_candidates(bind)
    op.create_index("ix_review_queue", "review_item", ["status", "priority", "created_at"])
    op.create_index("uq_review_open_pair", "review_item", ["pair_key"], unique=True,
                    sqlite_where=sa.text("status = 'open' AND pair_key IS NOT NULL"),
                    postgresql_where=sa.text("status = 'open' AND pair_key IS NOT NULL"))
    if CLEARED_KEPT in tables:
        _restore_cleared(bind)
    if VARIANT_KEPT in tables:
        _restore_variants(bind)


def _create_tables() -> None:
    op.create_table(
        "match_decision",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("observation_id", sa.String(26), sa.ForeignKey("observation.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("source_scope", sa.String(512), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=False),
        sa.Column("canonical_id", sa.String(40), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("margin", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("merges", sa.JSON(), nullable=True),
        sa.Column("matcher", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False, server_default="auto"),
        sa.Column("review_id", sa.String(26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_match_decision_cid", "match_decision", ["canonical_id", "created_at"])
    op.create_index("ix_match_decision_chain", "match_decision", ["source", "source_scope", "external_id",
                                                                  "created_at"])
    op.create_table(
        "match_rejection",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("a", sa.String(80), nullable=False),
        sa.Column("b", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("local_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("origin_node", sa.String(64), nullable=True),
        sa.Column("origin_seq", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(32), nullable=True),
    )
    op.create_index("uq_match_rejection_active", "match_rejection", ["scope", "a", "b"], unique=True,
                    sqlite_where=sa.text("ended_at IS NULL"), postgresql_where=sa.text("ended_at IS NULL"))
    op.create_index("ix_match_rejection_b", "match_rejection", ["b"],
                    sqlite_where=sa.text("ended_at IS NULL"), postgresql_where=sa.text("ended_at IS NULL"))
    op.create_table(
        "merge_record",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("src", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("dst", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("cause", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("decision_id", sa.String(26), nullable=True),
        sa.Column("review_id", sa.String(26), nullable=True),
        sa.Column("origin_node", sa.String(64), nullable=True),
        sa.Column("origin_seq", sa.BigInteger(), nullable=True),
        sa.Column("feed_seq", sa.BigInteger(), nullable=True),
        sa.Column("src_view", sa.JSON(), nullable=False),
        sa.Column("src_explain", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("late_chains", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_by", sa.String(128), nullable=True),
    )
    op.create_index("uq_merge_record_live", "merge_record", ["src"], unique=True,
                    sqlite_where=sa.text("reverted_at IS NULL"), postgresql_where=sa.text("reverted_at IS NULL"))
    op.create_index("ix_merge_record_dst", "merge_record", ["dst", "created_at"])
    op.create_table(
        "merge_record_item",
        sa.Column("merge_id", sa.String(26), sa.ForeignKey("merge_record.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("n", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("ref", sa.String(900), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("data_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_merge_record_item_kind", "merge_record_item", ["kind", "merge_id"])


def _live_map(bind) -> dict[str, str]:
    """Every merged record id -> the live record at the end of its merge chain (bounded, cycle safe)."""
    parent = {r.id: r.merged_into for r in bind.execute(
        sa.select(canonical.c.id, canonical.c.merged_into).where(canonical.c.merged_into.is_not(None)))}
    out: dict[str, str] = {}
    for cid in parent:
        cur = cid
        for _ in range(MAX_HOPS):
            nxt = parent.get(cur)
            if nxt is None:
                break
            cur = nxt
        out[cid] = cur
    return out


def _backfill_record_live(bind) -> None:
    rows = [{"id": k, "live_id": v} for k, v in sorted(_live_map(bind).items())]
    for i in range(0, len(rows), BATCH):
        bind.execute(record_live.insert(), rows[i:i + BATCH])


def pair_key(kind: str, ids: list[str]) -> str:
    """§11.5 rule 1 (the same function as `matching.pair_key`, frozen here so the migration never changes)."""
    return hashlib.sha256((kind + "\n" + "\n".join(sorted(set(ids)))).encode()).hexdigest()[:32]


def _backfill_reviews(bind) -> None:
    live = _live_map(bind)
    seen: set[str] = set()
    rows = bind.execute(sa.select(review.c.id, review.c.kind, review.c.canonical_id, review.c.candidates,
                                  review.c.confidence, review.c.pair_key).where(review.c.status == "open")
                        .order_by(review.c.created_at, review.c.id)).all()
    for row in rows:
        cands = _json(row.candidates) if not isinstance(row.candidates, list) else row.candidates
        ids = [live.get(row.canonical_id, row.canonical_id)]
        ids += [live.get(c[0], c[0]) for c in cands or [] if isinstance(c, list) and c]
        # An item restored from REVIEW_KEPT keeps its own pair (an `unmerge_check` pair names its merge as well).
        key = row.pair_key or pair_key(row.kind, ids)
        priority = PRIORITY.get(row.kind, 10)
        if row.kind == "possible_duplicate":
            priority += round(float(row.confidence or 0) * 10)
        if key in seen or len(set(ids)) < 2:  # a duplicate of an older open item, or nothing left to compare
            bind.execute(review.update().where(review.c.id == row.id).values(status="superseded", priority=priority))
            continue
        seen.add(key)
        bind.execute(review.update().where(review.c.id == row.id).values(pair_key=key, priority=priority))


def _backfill_review_candidates(bind) -> None:
    """`review_candidate` from every item's `candidates` (in id order, batched)."""
    last = ""
    while True:
        rows = bind.execute(sa.select(review.c.id, review.c.candidates).where(review.c.id > last)
                            .order_by(review.c.id).limit(BATCH)).all()
        if not rows:
            break
        out = []
        for row in rows:
            cands = row.candidates if isinstance(row.candidates, list) else _json(row.candidates)
            ids = {c[0][:40] for c in (cands if isinstance(cands, list) else [])[:100]
                   if isinstance(c, list) and c and isinstance(c[0], str) and c[0]}
            out += [{"review_id": row.id, "canonical_id": cid} for cid in sorted(ids)]
        if out:
            bind.execute(review_candidate.insert(), out)
        last = rows[-1].id


def _review_kept():
    return sa.table(REVIEW_KEPT, sa.column("id", sa.String), sa.column("status", sa.String),
                    sa.column("reason", sa.String), sa.column("evidence", sa.JSON),
                    sa.column("decision_id", sa.String), sa.column("pair_key", sa.String),
                    sa.column("priority", sa.Integer), sa.column("resolved_at", sa.DateTime(timezone=True)))


def _restore_reviews(bind) -> None:
    """Put back what the downgrade kept aside, on items that still exist. An item the downgrade hid (its `status`
    kept) is reopened unless an item restored before it holds its pair; an item the previous release resolved
    stays resolved. Open items the previous release added get their pair from `_backfill_reviews`, which keeps
    the older of two open items for one pair (§11.5 rule 1)."""
    kept = _review_kept()
    open_pairs = set()
    for row in bind.execute(sa.select(kept).order_by(kept.c.id)).all():
        current = bind.execute(sa.select(review.c.status).where(review.c.id == row.id)).first()
        if current is None:
            continue
        values = {"reason": row.reason, "evidence": _json(row.evidence) if row.evidence is not None else None,
                  "decision_id": row.decision_id, "pair_key": row.pair_key, "priority": row.priority or 0,
                  "resolved_at": row.resolved_at}
        if row.status is not None and current.status == "superseded":  # hidden by the downgrade
            if not (row.pair_key and row.pair_key in open_pairs):
                values["status"] = row.status
        # A resolved item keeps its pair (a dismissal is found by it, §11.5 rule 3); only open ones are unique.
        if values.get("status", current.status) == "open" and values["pair_key"]:
            open_pairs.add(values["pair_key"])
        bind.execute(review.update().where(review.c.id == row.id).values(**values))
    op.drop_table(REVIEW_KEPT)


def _restore_seen(bind) -> None:
    kept = sa.table(SEEN_KEPT, sa.column("id", sa.String), sa.column("last_seen_at", sa.DateTime(timezone=True)))
    last = ""
    while True:
        rows = bind.execute(sa.select(kept).where(kept.c.id > last).order_by(kept.c.id).limit(BATCH)).all()
        if not rows:
            break
        for row in rows:
            bind.execute(observation.update().where(observation.c.id == row.id).values(last_seen_at=row.last_seen_at))
        last = rows[-1].id
    op.drop_table(SEEN_KEPT)


def _restore_cleared(bind) -> None:
    kept = sa.table(CLEARED_KEPT, sa.column("canonical_id", sa.String), sa.column("field", sa.String),
                    sa.column("set_by", sa.String), sa.column("local_only", sa.Boolean),
                    sa.column("created_at", sa.DateTime))
    live = _live_map(bind)
    for row in bind.execute(sa.select(kept)).all():
        cid = live.get(row.canonical_id, row.canonical_id)
        taken = bind.execute(sa.select(lock.c.field).where(lock.c.canonical_id == cid,
                                                           lock.c.field == row.field)).first()
        if taken is not None:  # the previous release locked the field meanwhile: that newer decision stays
            continue
        bind.execute(lock.insert().values(canonical_id=cid, field=row.field, value=None, set_by=row.set_by,
                                          local_only=row.local_only, created_at=row.created_at, cleared=True))
    op.drop_table(CLEARED_KEPT)


def _restore_variants(bind) -> None:
    kept = sa.table(VARIANT_KEPT, sa.column("namespace", sa.String), sa.column("external_id", sa.String),
                    sa.column("variant", sa.String))
    for row in bind.execute(sa.select(kept)).all():
        bind.execute(crosswalk.update().where(crosswalk.c.namespace == row.namespace,
                                              crosswalk.c.external_id == row.external_id)
                     .values(variant=row.variant))
    op.drop_table(VARIANT_KEPT)


#: Phase 4 defect D3: a re-upgrade re-derives what this downgrade removed (`atlas.dates_rebuild.finish_pending`
#: reads this key; spelled out here because a migration never imports application code that may change later).
REBUILD_KEY = "view_rebuild_pending"


def _ask_for_rebuild(bind) -> None:
    """Leave the node a note: once the schema is current again, the application re-derives every view."""
    state = sa.table("node_state", sa.column("key", sa.String), sa.column("value", sa.Text))
    bind.execute(state.delete().where(state.c.key == REBUILD_KEY))
    bind.execute(state.insert().values(key=REBUILD_KEY, value="pending"))


def downgrade() -> None:
    bind = op.get_bind()
    _ask_for_rebuild(bind)  # explain._meta and field_conflict are derived: rebuilt after the re-upgrade
    # The previous release lists every explain key on the game page: remove the v2 metadata key.
    last = ""
    while True:
        rows = bind.execute(sa.select(canonical.c.id, canonical.c.explain).where(canonical.c.id > last)
                            .order_by(canonical.c.id).limit(BATCH)).all()
        if not rows:
            break
        for row in rows:
            explain = _json(row.explain)
            if isinstance(explain, dict) and "_meta" in explain:
                bind.execute(canonical.update().where(canonical.c.id == row.id)
                             .values(explain={k: v for k, v in explain.items() if k != "_meta"}))
        last = rows[-1].id

    op.create_table(
        CLEARED_KEPT,
        sa.Column("canonical_id", sa.String(40), primary_key=True),
        sa.Column("field", sa.String(50), primary_key=True),
        sa.Column("set_by", sa.String(128), nullable=False),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    bind.execute(sa.text(f"INSERT INTO {CLEARED_KEPT} (canonical_id, field, set_by, local_only, created_at) "
                         "SELECT canonical_id, field, set_by, local_only, created_at FROM field_lock "
                         "WHERE cleared = :t"), {"t": True})
    bind.execute(sa.text("DELETE FROM field_lock WHERE cleared = :t"), {"t": True})
    op.create_table(
        VARIANT_KEPT,
        sa.Column("namespace", sa.String(50), primary_key=True),
        sa.Column("external_id", sa.String(256), primary_key=True),
        sa.Column("variant", sa.String(120), nullable=False),
    )
    bind.execute(sa.text(f"INSERT INTO {VARIANT_KEPT} (namespace, external_id, variant) "
                         "SELECT namespace, external_id, variant FROM crosswalk WHERE variant <> ''"))

    # Review columns (and the status of open items of kinds the previous release does not know) go aside, so a
    # re-upgrade keeps dismissal windows (`resolved_at`), evidence and decision links.
    op.create_table(
        REVIEW_KEPT,
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("decision_id", sa.String(26), nullable=True),
        sa.Column("pair_key", sa.String(32), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    kinds = ", ".join(f"'{k}'" for k in OLD_KINDS)
    hidden = f"(status = 'open' AND kind NOT IN ({kinds}))"
    bind.execute(sa.text(
        f"INSERT INTO {REVIEW_KEPT} (id, status, reason, evidence, decision_id, pair_key, priority, resolved_at) "
        f"SELECT id, CASE WHEN {hidden} THEN status ELSE NULL END, reason, evidence, decision_id, pair_key, "
        "priority, resolved_at FROM review_item WHERE reason IS NOT NULL OR evidence IS NOT NULL "
        f"OR decision_id IS NOT NULL OR pair_key IS NOT NULL OR resolved_at IS NOT NULL OR priority <> 0 OR {hidden}"))
    bind.execute(sa.text(f"UPDATE review_item SET status = 'superseded' WHERE {hidden}"))
    op.create_table(
        SEEN_KEPT,
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind.execute(sa.text(f"INSERT INTO {SEEN_KEPT} (id, last_seen_at) SELECT id, last_seen_at FROM observation "
                         "WHERE last_seen_at IS NOT NULL"))

    op.drop_index("uq_review_open_pair", table_name="review_item")
    op.drop_index("ix_review_queue", table_name="review_item")
    with op.batch_alter_table("review_item") as batch:
        for column in ("resolved_at", "priority", "pair_key", "decision_id", "evidence", "reason"):
            batch.drop_column(column)
    with op.batch_alter_table("observation") as batch:
        batch.drop_column("last_seen_at")
        batch.drop_column("payload_hash")
    with op.batch_alter_table("crosswalk") as batch:
        batch.drop_column("variant")
    with op.batch_alter_table("field_lock") as batch:
        batch.drop_column("cleared")

    op.drop_index("ix_review_candidate_cid", table_name="review_candidate")
    op.drop_table("review_candidate")
    op.drop_index("ix_field_conflict_field", table_name="field_conflict")
    op.drop_table("field_conflict")
    op.drop_index("ix_record_live_live_id", table_name="record_live")
    op.drop_table("record_live")
    for name in KEPT_TABLES:
        op.rename_table(name, f"{name}_0034_kept")
