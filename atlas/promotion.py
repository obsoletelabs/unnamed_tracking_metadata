"""Build an offline recovery candidate from one verified, retained authority feed."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sqlalchemy import delete, func, select, text

from . import backup, client_changes, custom, db, feed, feed_schema, key_rotation, sync
from .models import ClientChange, FieldDef, MediaType, NodeState, ReplicatedChange, ReplicatedFeed, SyncCursor, SyncFailure
from .service import AtlasError, Ctx
from .settings import Settings
from .signing import load_identity

KINDS = feed_schema.known_kinds()


class PromotionError(ValueError):
    pass


def candidate(settings: Settings, source_engine, authority: str, confirm_mirror: str, out: Path) -> dict:
    """Read a consistent public-feed snapshot; never change or serve the source mirror.

    The archive has a new private signing key, but no accounts, tokens, peer credentials or
    unrelated local/provider records. The operator must fence the old writer before cutover.
    """
    out = Path(out)
    if settings.role != "mirror":
        raise PromotionError("recovery candidates must be built from a mirror")
    if out.exists() or out.with_name(out.name + ".part").exists():
        raise PromotionError("the output path already exists; choose a new archive name")
    maker = db.make_sessionmaker(source_engine)
    with maker() as source:
        if source_engine.dialect.name == "postgresql":
            source.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif source_engine.dialect.name == "sqlite":
            source.execute(text("BEGIN"))
        mirror = source.get(NodeState, "node_id")
        if mirror is None or confirm_mirror != mirror.value:
            raise PromotionError("confirm the source mirror's exact node ID")
        root = source.get(ReplicatedFeed, authority)
        cursor = source.get(SyncCursor, authority)
        head = source.scalar(select(func.max(ReplicatedChange.seq)).where(ReplicatedChange.node_id == authority)) or 0
        if root is None or cursor is None or not head or cursor.last_seq != head:
            raise PromotionError("the selected authority has no complete retained feed; backfill it before recovery")
        parked = source.scalar(select(func.count()).select_from(SyncFailure).where(
            SyncFailure.node_id == authority, SyncFailure.disposition == feed_schema.PARK,
            SyncFailure.resolved_at.is_(None))) or 0
        if parked:
            # applied == verified is not enough: a parked entry was never applied (C1.7).
            raise PromotionError(f"{parked} retained entries of that authority are parked (they need a newer "
                                 "release); upgrade and run `atlas sync replay` before recovery")
        history = key_rotation.trusted_history(source, authority)
        tip = history[-1]["body"]["key_id"] if history else authority
        try:
            history, keys = key_rotation.validate_history(authority, root.public_key, history, tip)
        except key_rotation.RotationError as exc:
            raise PromotionError(str(exc)) from exc
        if history and history[-1]["seq"] > head:
            raise PromotionError("key history is ahead of the retained feed; finish synchronization first")
        with tempfile.TemporaryDirectory(prefix="atlas-recovery-") as folder:
            target_settings = Settings(_env_file=None, database_url=f"sqlite:///{folder}/candidate.db", role="authority",
                                       environment="dev", public_url="http://localhost:8000",
                                       source_trust=settings.source_trust, default_trust=settings.default_trust,
                                       media_dir=f"{folder}/media", media_cache_enabled=False)
            db.upgrade(target_settings.database_url)
            target_engine = db.make_engine(target_settings.database_url)
            target_maker = db.make_sessionmaker(target_engine)
            try:
                with target_maker() as target:
                    identity = load_identity(target)
                    ctx = Ctx(target_settings, identity)
                    custom.seed(target)
                    target.add(ReplicatedFeed(node_id=authority, public_key=root.public_key))
                    target.flush()
                    expected = 1
                    tail = None
                    rows = source.scalars(select(ReplicatedChange).where(ReplicatedChange.node_id == authority)
                                          .order_by(ReplicatedChange.seq)).yield_per(500)
                    for row in rows:
                        entry = key_rotation.proof(row, authority)
                        if row.seq != expected or not key_rotation.check_entry(authority, history, keys, entry):
                            raise PromotionError(f"invalid signature, gap or fork at retained sequence {expected}")
                        if row.kind not in KINDS:
                            raise PromotionError(f"unsupported feed kind {row.kind!r}; upgrade before recovery")
                        outcome = feed_schema.classify(row.kind, row.body)
                        if outcome.action == feed_schema.PARK:
                            raise PromotionError(f"retained entry {row.seq} is not supported by this release "
                                                 f"({outcome.message}); upgrade before recovery")
                        target.add(ReplicatedChange(node_id=authority, seq=row.seq, kind=row.kind, body=row.body,
                                                    body_hash=row.body_hash, signature=row.signature, key_id=row.key_id))
                        target.flush()
                        if row.kind != "key_rotation":
                            # Use the same replay contract as a follower, then sign the original public payload
                            # under the new independent identity. Local/private state is never copied.
                            seq = feed.append(target, identity, row.kind, row.body)
                            if outcome.action == feed_schema.APPLY:
                                try:
                                    with target.begin_nested():
                                        sync._apply(target, ctx, identity.node_id, seq, row.kind, outcome.body)
                                except (sync.InvalidEntry, AtlasError):
                                    pass  # as on the source mirror: an entry it could not apply stays unapplied
                        expected += 1
                        tail = row.body_hash
                    if expected != head + 1:
                        raise PromotionError("retained feed changed or is incomplete")
                    # Recovered schema belongs to the replacement authority and can be administered there.
                    for model in (MediaType, FieldDef):
                        for definition in target.scalars(select(model)):
                            definition.origin, definition.origin_node = "local", None
                    # The client change log is local to a node: the replay above wrote rows for the candidate's
                    # own rebuilds, and the source mirror's log is never read. Start the new node empty (C3.1).
                    target.execute(delete(ClientChange))
                    client_changes.renew_epoch(target)
                    target.add(SyncCursor(upstream=authority, last_seq=head))
                    key_rotation.remember(target, authority, history)
                    provenance = {"source_mirror": confirm_mirror, "recovered_authority": authority,
                                  "recovered_through_seq": head, "tail_body_hash": tail,
                                  "node_id": identity.node_id, "public_key": identity.trust_public_b64}
                    target.add(NodeState(key="recovery_provenance", value=json.dumps(provenance, sort_keys=True)))
                    target.commit()
                backup.create(target_settings, target_engine, out, include_media=False)
                backup.verify(out)
                return provenance
            finally:
                target_engine.dispose()
