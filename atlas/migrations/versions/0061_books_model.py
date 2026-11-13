"""the book model (CONTRACT-C15 §2, A21)

Revision ID: 0061
Revises: 0060

Upgrade:
* `relation.position` (String(16), nullable): a `series_member`'s place in its series (C15 §2.4). Existing rows get
  NULL (no position), so every relation reads and signs exactly as before.
* `book_creator` (canonical_id, position, name, norm_name, role): a book's ordered creators, derived from the view on
  every rebuild (C15 §5), at most 30 rows per record. Filled for existing book records below.
* A node that already has a media type `book` (made by an administrator, or inherited from an upstream) keeps its
  label, labels and custom fields: the built-in seed never touches an existing key. The migration only turns its
  achievements flag off (achievements do not apply to books, C15 §2.7) and records a notice (an audit entry and the
  `notice.media_types_adopted` setting, shown on Admin -> Media types). A custom field whose key is also a book view
  key (`authors`, `subtitle`, `page_count`, `original_language`, `editions`) keeps its values under `custom`.
* `canonical.norm_title` is recomputed for existing `book` records: a book's matching key now keeps its "Edition"
  words (C15 §2.5), and the stored, indexed column would otherwise match against the old key.

Downgrade: the column and the table are dropped. The achievements flag and the recomputed keys are left as they are
(the flag is the administrator's to change; the old keys come back on the next rebuild of each record).

Numbering: 0061 is deliberately far from the live head (0037) because other programme lanes add migrations in
parallel; it is renumbered when the chain is restacked. `down_revision` is the head of this branch's base (A20).
"""
from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = '0061'
down_revision = '0060'
branch_labels = None
depends_on = None

BOOK_VIEW_KEYS = ("authors", "subtitle", "page_count", "original_language", "editions")
NOTICE = "notice.media_types_adopted"


def upgrade() -> None:
    with op.batch_alter_table("relation") as batch:
        batch.add_column(sa.Column("position", sa.String(16), nullable=True))
    op.create_table(
        "book_creator",
        sa.Column("canonical_id", sa.String(40), sa.ForeignKey("canonical.id"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("norm_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
    )
    op.create_index("ix_book_creator_norm_name", "book_creator", ["norm_name"])

    bind = op.get_bind()
    meta = sa.MetaData()
    media_type = sa.Table("media_type", meta, autoload_with=bind)
    field_def = sa.Table("field_def", meta, autoload_with=bind)
    canonical = sa.Table("canonical", meta, autoload_with=bind)
    audit = sa.Table("audit_entry", meta, autoload_with=bind)
    setting = sa.Table("setting", meta, autoload_with=bind)
    creators = sa.Table("book_creator", meta, autoload_with=bind)

    existing = bind.execute(sa.select(media_type.c.key, media_type.c.label, media_type.c.origin)
                            .where(media_type.c.key == "book")).first()
    if existing is not None:
        fields = [r.key for r in bind.execute(sa.select(field_def.c.key).where(field_def.c.media_type == "book"))]
        bind.execute(media_type.update().where(media_type.c.key == "book").values(achievements=False))
        from atlas.ids import ulid

        detail = {"key": "book", "label": existing.label, "origin": existing.origin, "fields": len(fields),
                  "shadowed": sorted(set(fields) & set(BOOK_VIEW_KEYS))}
        at = datetime.now(UTC)
        bind.execute(audit.insert().values(id=ulid(), actor_id=None, action="media_type_builtin", target="book",
                                           detail=detail, created_at=at))
        held = bind.execute(sa.select(setting.c.value).where(setting.c.key == NOTICE)).first()
        if held is None:
            bind.execute(setting.insert().values(key=NOTICE, value=[detail], updated_by=None, updated_at=at))
        else:  # an upgrade after a downgrade: one notice per type
            notices = [n for n in (held.value if isinstance(held.value, list) else [])
                       if not (isinstance(n, dict) and n.get("key") == "book")]
            bind.execute(setting.update().where(setting.c.key == NOTICE).values(value=[*notices, detail],
                                                                                 updated_at=at))

    from atlas.books import norm_name
    from atlas.normalize import norm_title

    rows = bind.execute(sa.select(canonical.c.id, canonical.c.view, canonical.c.merged_into)
                        .where(canonical.c.media_type == "book")).all()
    for cid, view, merged_into in rows:
        view = view or {}
        bind.execute(canonical.update().where(canonical.c.id == cid)
                     .values(norm_title=norm_title(view.get("title"), "book")))
        authors = view.get("authors") if merged_into is None else None
        for position, author in enumerate(authors[:30] if isinstance(authors, list) else []):
            if isinstance(author, dict) and isinstance(author.get("name"), str) and author["name"].strip():
                bind.execute(creators.insert().values(canonical_id=cid, position=position, name=author["name"][:200],
                                                      norm_name=norm_name(author["name"]),
                                                      role=str(author.get("role") or "author")[:16]))


def downgrade() -> None:
    op.drop_index("ix_book_creator_norm_name", table_name="book_creator")
    op.drop_table("book_creator")
    with op.batch_alter_table("relation") as batch:
        batch.drop_column("position")
