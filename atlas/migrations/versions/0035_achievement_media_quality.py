"""achievement and media quality: sets, retirement, translations, rarity freshness, image quality (C12, A18)

Revision ID: 0035
Revises: 0034

Contract C12 plans three migrations (0033 sets, 0034 texts and stats, 0035 media). Those numbers were taken by
A16 and A17, and this release ships the three together, so they are one migration (CONTRACT-AMENDMENTS A18 #1).

Upgrade:
* `achievement` gains `set_key` (default `base`), `source_order`, `status`, `retired_at`, `text_locale`,
  `percent_as_of`, `icon_licence`, `icon_attribution`, `icon_last_error` and `locked_icon_last_error`.
* F1 (§12.1.4, amended): the unique index `uq_achievement (canonical_id, source, norm_name)` is replaced by
  `uq_achievement_ident (canonical_id, source, external_id, norm_name)`. Every existing row satisfies it (the old
  index was stricter), so no row changes. `uq_achievement_key` is unchanged.
* New tables `achievement_set`, `achievement_text` and the local-only `achievement_stat`.
* `artwork` gains `declared_width`/`declared_height` (backfilled from today's `width`/`height`), `aspect`,
  `dhash`, `duplicate_of`, `region`, `locale`, `licence`, `attribution`, `source_page_url`, `last_error` and
  `local_only`, and the index `ix_artwork_dup`. Measuring cached images (aspect, dhash, measured size, exact
  duplicates) is not done here: the media worker does it in bounded passes (`atlas media backfill`).

Existing data: every achievement is in its source's `base` set and stays active; `source_order` is filled by the
next write of each list. No key, id, record version or signed feed entry changes.

Downgrade keeps the data aside (the A15-A17 pattern; CONTRACT-AMENDMENTS A18 #2) and needs no flag:
* `achievement_set`, `achievement_text` and `achievement_stat` are renamed `*_0035_kept`.
* Rows the previous release must not see are moved to `achievement_rows_0035_kept` (the whole row and its
  translations, as JSON) and deleted: retired rows (it has no notion of retirement and would list them as
  active), and, in one `(record, source)`, every row after the first of a name (its `uq_achievement` allows one
  per name; the first is the lowest `(source_order, id)`, NULL `source_order` last).
* Curated artwork rows whose licence keeps them out of the CC0 feed (`cc-by`, `cc-by-sa`, `publisher_press`,
  `provider_terms`) or that are `local_only` are moved to `artwork_rows_0035_kept` with their votes: the previous
  release would sign them with the next write of their source.
* Curated achievement rows whose `icon_licence` is withheld lose their icon addresses (and cached icons) for as
  long as the previous release runs, for the same reason: it would sign them into the CC0 feed with the next write
  of the list. The addresses are kept with the row's new columns.
* The new columns of the remaining rows go to `achievement_cols_0035_kept` / `artwork_cols_0035_kept`, the
  declared image size included; then the columns are dropped and `uq_achievement` is re-created.
Upgrading again restores all of it. A withheld icon comes back only when the previous release left the row's
icons empty, and a declared size only when the row's width and height are still the measured ones it kept. Rows of
records merged meanwhile follow the merge; a set-aside row whose key or identity the previous release gave to a new
row in the meantime stays out (the newer row wins).
"""
from __future__ import annotations

import json
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None

TABLES = ("achievement_set", "achievement_text", "achievement_stat")
ACH_ROWS = "achievement_rows_0035_kept"
ACH_COLS = "achievement_cols_0035_kept"
ART_ROWS = "artwork_rows_0035_kept"
ART_COLS = "artwork_cols_0035_kept"
#: Sources whose artwork enters the signed feed (curated); provider rows never do.
CURATED = ("curator", "community", "manual")
WITHHELD = ("cc-by", "cc-by-sa", "publisher_press", "provider_terms")
MAX_HOPS = 32
BATCH = 1000

ICONS = ("icon_url", "locked_icon_url", "icon_blob_hash", "locked_icon_blob_hash")
ACH_NEW = (("set_key", sa.String(64)), ("source_order", sa.Integer()), ("status", sa.String(16)),
           ("retired_at", sa.DateTime(timezone=True)), ("text_locale", sa.String(35)), ("percent_as_of", sa.Date()),
           ("icon_licence", sa.String(24)), ("icon_attribution", sa.String(300)),
           ("icon_last_error", sa.String(32)), ("locked_icon_last_error", sa.String(32)))
ART_NEW = (("declared_width", sa.Integer()), ("declared_height", sa.Integer()), ("aspect", sa.String(12)),
           ("dhash", sa.String(16)), ("duplicate_of", sa.String(26)), ("region", sa.String(8)),
           ("locale", sa.String(35)), ("licence", sa.String(24)), ("attribution", sa.String(300)),
           ("source_page_url", sa.String(1000)), ("last_error", sa.String(32)), ("local_only", sa.Boolean()))
ACH_OLD = (("id", sa.String(26)), ("canonical_id", sa.String(40)), ("source", sa.String(128)),
           ("external_id", sa.String(200)), ("name", sa.String(200)), ("norm_name", sa.String(200)),
           ("description", sa.Text()), ("icon_url", sa.String(1000)), ("hidden", sa.Boolean()),
           ("global_percent", sa.Float()), ("redistributable", sa.Boolean()), ("kind", sa.String(16)),
           ("tier", sa.String(8)), ("points", sa.Integer()), ("locked_icon_url", sa.String(1000)),
           ("icon_blob_hash", sa.String(64)), ("icon_fail_count", sa.Integer()),
           ("icon_next_try_at", sa.DateTime(timezone=True)), ("locked_icon_blob_hash", sa.String(64)),
           ("locked_icon_fail_count", sa.Integer()), ("locked_icon_next_try_at", sa.DateTime(timezone=True)),
           ("position", sa.Integer()), ("key", sa.String(30)))
ART_OLD = (("id", sa.String(26)), ("canonical_id", sa.String(40)), ("source", sa.String(128)),
           ("kind", sa.String(16)), ("url", sa.String(1000)), ("width", sa.Integer()), ("height", sa.Integer()),
           ("priority", sa.Integer()), ("redistributable", sa.Boolean()), ("blob_hash", sa.String(64)),
           ("content_type", sa.String(32)), ("size_bytes", sa.Integer()), ("cached_at", sa.DateTime(timezone=True)),
           ("fail_count", sa.Integer()), ("next_try_at", sa.DateTime(timezone=True)),
           ("created_at", sa.DateTime(timezone=True)), ("preferred", sa.Boolean()))
VOTE = (("artwork_id", sa.String(26)), ("user_id", sa.String(26)), ("value", sa.Integer()),
        ("created_at", sa.DateTime(timezone=True)))


def _table(name: str, columns) -> sa.TableClause:
    return sa.table(name, *(sa.column(c, t) for c, t in columns))


achievement_new = _table("achievement", ACH_OLD + ACH_NEW)
achievement_old = _table("achievement", ACH_OLD)
artwork_new = _table("artwork", ART_OLD + ART_NEW)
artwork_old = _table("artwork", ART_OLD)
vote = _table("artwork_vote", VOTE)
text_t = _table("achievement_text", (("achievement_id", sa.String(26)), ("locale", sa.String(35)),
                                     ("name", sa.String(200)), ("description", sa.Text())))
canonical = _table("canonical", (("id", sa.String(40)), ("merged_into", sa.String(40))))
app_user = _table("app_user", (("id", sa.String(26)),))
kept_rows = _table("_", (("id", sa.String(26)), ("canonical_id", sa.String(40)), ("data", sa.JSON())))
kept_cols = _table("_", (("id", sa.String(26)), ("data", sa.JSON())))


def _kept(name: str, template: sa.TableClause) -> sa.TableClause:
    return sa.table(name, *(sa.column(c.name, c.type) for c in template.columns))


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _typed(columns, data: dict) -> dict:
    """JSON values back to what each column type takes (dates and timestamps come back as ISO strings)."""
    out = {}
    for name, type_ in columns:
        if name not in data:
            continue
        value = data[name]
        if isinstance(value, str) and isinstance(type_, sa.DateTime):
            value = datetime.fromisoformat(value)
        elif isinstance(value, str) and isinstance(type_, sa.Date):
            value = date.fromisoformat(value[:10])
        out[name] = value
    return out


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _live(bind, cid: str) -> str | None:
    """The live record `cid` was merged into (following the chain), or None when the record is gone."""
    for _ in range(MAX_HOPS):
        row = bind.execute(sa.select(canonical.c.id, canonical.c.merged_into).where(canonical.c.id == cid)).first()
        if row is None:
            return None
        if not row.merged_into:
            return cid
        cid = row.merged_into
    return None


# ---- upgrade ------------------------------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    restoring = all(f"{t}_0035_kept" in tables for t in TABLES)
    if restoring:
        for name in TABLES:
            op.rename_table(f"{name}_0035_kept", name)
    else:
        _create_tables()

    with op.batch_alter_table("achievement") as batch:
        batch.add_column(sa.Column("set_key", sa.String(64), nullable=False, server_default="base"))
        batch.add_column(sa.Column("source_order", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("text_locale", sa.String(35), nullable=True))
        batch.add_column(sa.Column("percent_as_of", sa.Date(), nullable=True))
        batch.add_column(sa.Column("icon_licence", sa.String(24), nullable=True))
        batch.add_column(sa.Column("icon_attribution", sa.String(300), nullable=True))
        batch.add_column(sa.Column("icon_last_error", sa.String(32), nullable=True))
        batch.add_column(sa.Column("locked_icon_last_error", sa.String(32), nullable=True))
        batch.drop_constraint("uq_achievement", type_="unique")
        batch.create_index("uq_achievement_ident", ["canonical_id", "source", "external_id", "norm_name"],
                           unique=True)
        batch.create_index("ix_achievement_retired_at", ["retired_at"])
    with op.batch_alter_table("artwork") as batch:
        batch.add_column(sa.Column("declared_width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("declared_height", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("aspect", sa.String(12), nullable=True))
        batch.add_column(sa.Column("dhash", sa.String(16), nullable=True))
        batch.add_column(sa.Column("duplicate_of", sa.String(26), nullable=True))
        batch.add_column(sa.Column("region", sa.String(8), nullable=True))
        batch.add_column(sa.Column("locale", sa.String(35), nullable=True))
        batch.add_column(sa.Column("licence", sa.String(24), nullable=True))
        batch.add_column(sa.Column("attribution", sa.String(300), nullable=True))
        batch.add_column(sa.Column("source_page_url", sa.String(1000), nullable=True))
        batch.add_column(sa.Column("last_error", sa.String(32), nullable=True))
        batch.add_column(sa.Column("local_only", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_artwork_dup", ["canonical_id", "kind", "blob_hash"])
    # §12.5.2: until an image is measured, its declared size is what width/height hold.
    bind.execute(artwork_new.update().values(declared_width=artwork_new.c.width,
                                             declared_height=artwork_new.c.height))
    if restoring:
        _restore(bind, tables)


def _create_tables() -> None:
    op.create_table(
        "achievement_set",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("canonical_id", sa.String(40), sa.ForeignKey("canonical.id"), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("set_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("dlc_canonical_id", sa.String(40), nullable=True),
        sa.Column("edition", sa.String(100), nullable=True),
        sa.Column("platforms", sa.JSON(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_achievement_set", "achievement_set", ["canonical_id", "source", "set_key"], unique=True)
    op.create_index("ix_achievement_set_dlc", "achievement_set", ["dlc_canonical_id"])
    op.create_table(
        "achievement_text",
        sa.Column("achievement_id", sa.String(26), sa.ForeignKey("achievement.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("locale", sa.String(35), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )
    op.create_table(
        "achievement_stat",
        sa.Column("canonical_id", sa.String(40), sa.ForeignKey("canonical.id"), primary_key=True),
        sa.Column("source", sa.String(128), primary_key=True),
        sa.Column("external_id", sa.String(200), primary_key=True),
        sa.Column("percent", sa.Float(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("percent >= 0 AND percent <= 100", name="ck_achievement_stat_percent"),
    )


def _restore(bind, tables: set[str]) -> None:
    """Put back what the downgrade set aside, on the records as they are now."""
    ach_ids = {r.id for r in bind.execute(sa.select(achievement_new.c.id))}
    if ACH_COLS in tables:
        for row in bind.execute(sa.select(_kept(ACH_COLS, kept_cols))).all():
            if row.id in ach_ids:
                data = _json(row.data)
                values = _typed(ACH_NEW, data)
                icons = data.get("_icons")
                if icons:  # withheld icons set aside; the previous release may have given the row new ones
                    now = bind.execute(sa.select(*(achievement_new.c[c] for c in ICONS[:2])).where(
                        achievement_new.c.id == row.id)).first()
                    if now is not None and not any(now):
                        values.update({c: icons.get(c) for c in ICONS})
                if values:
                    bind.execute(achievement_new.update().where(achievement_new.c.id == row.id).values(**values))
        op.drop_table(ACH_COLS)
    if ACH_ROWS in tables:
        for row in bind.execute(sa.select(_kept(ACH_ROWS, kept_rows)).order_by(sa.text("id"))).all():
            data = _json(row.data)
            live = _live(bind, data["canonical_id"])
            clash = live is None or bind.execute(sa.select(achievement_new.c.id).where(
                achievement_new.c.canonical_id == live,
                (achievement_new.c.id == data["id"]) | (achievement_new.c.key == data["key"])
                | ((achievement_new.c.source == data["source"])
                   & (achievement_new.c.external_id == data["external_id"])
                   & (achievement_new.c.norm_name == data["norm_name"])))).first()
            if clash:
                continue  # the previous release made a newer row for it meanwhile: that one stays
            bind.execute(achievement_new.insert().values(**{**_typed(ACH_OLD + ACH_NEW, data), "canonical_id": live}))
            for locale, text in (data.get("_texts") or {}).items():
                bind.execute(text_t.insert().values(achievement_id=data["id"], locale=locale, name=text["name"],
                                                    description=text.get("description", "")))
        op.drop_table(ACH_ROWS)
    if ART_COLS in tables:
        art_ids = {r.id for r in bind.execute(sa.select(artwork_new.c.id))}
        for row in bind.execute(sa.select(_kept(ART_COLS, kept_cols))).all():
            if row.id in art_ids:
                data = _json(row.data)
                values = _typed(ART_NEW, data)
                now = bind.execute(sa.select(artwork_new.c.width, artwork_new.c.height).where(
                    artwork_new.c.id == row.id)).first()
                if "_wh" not in data or now is None or [now.width, now.height] != data["_wh"]:
                    # the previous release wrote a new declaration into width/height: that one is the declaration
                    values.pop("declared_width", None), values.pop("declared_height", None)
                if values:
                    bind.execute(artwork_new.update().where(artwork_new.c.id == row.id).values(**values))
        op.drop_table(ART_COLS)
    if ART_ROWS in tables:
        users = {r.id for r in bind.execute(sa.select(app_user.c.id))}
        for row in bind.execute(sa.select(_kept(ART_ROWS, kept_rows)).order_by(sa.text("id"))).all():
            data = _json(row.data)
            live = _live(bind, data["canonical_id"])
            if live is None or bind.execute(sa.select(artwork_new.c.id).where(
                    (artwork_new.c.id == data["id"])
                    | ((artwork_new.c.canonical_id == live) & (artwork_new.c.kind == data["kind"])
                       & (artwork_new.c.url == data["url"])))).first():
                continue
            bind.execute(artwork_new.insert().values(**{**_typed(ART_OLD + ART_NEW, data), "canonical_id": live}))
            for v in data.get("_votes") or []:
                if v["user_id"] in users:
                    bind.execute(vote.insert().values(**_typed(VOTE, v)))
        op.drop_table(ART_ROWS)
    # Sets, translations and stats of records merged while downgraded follow the merge; leftovers that collide
    # with what the survivor already holds, or whose achievement is gone, are dropped.
    for name, keyed in (("achievement_set", ("source", "set_key")), ("achievement_stat", ("source", "external_id"))):
        t = sa.table(name, sa.column("canonical_id", sa.String), *(sa.column(k, sa.String) for k in keyed))
        merged = bind.execute(sa.select(t.c.canonical_id, *(t.c[k] for k in keyed)).select_from(
            t.join(canonical, canonical.c.id == t.c.canonical_id)).where(canonical.c.merged_into.is_not(None))).all()
        for row in merged:
            live = _live(bind, row.canonical_id)
            where = sa.and_(t.c.canonical_id == row.canonical_id, *(t.c[k] == getattr(row, k) for k in keyed))
            taken = live is None or bind.execute(sa.select(t.c.canonical_id).where(
                t.c.canonical_id == live, *(t.c[k] == getattr(row, k) for k in keyed))).first()
            if taken:
                bind.execute(t.delete().where(where))
            else:
                bind.execute(t.update().where(where).values(canonical_id=live))
    bind.execute(sa.text("DELETE FROM achievement_text WHERE achievement_id NOT IN (SELECT id FROM achievement)"))


# ---- downgrade ----------------------------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()
    for name in (ACH_ROWS, ACH_COLS, ART_ROWS, ART_COLS):
        op.create_table(name, sa.Column("id", sa.String(26), primary_key=True),
                        *([sa.Column("canonical_id", sa.String(40), nullable=False)] if name.endswith("rows_0035_kept")
                          else []),
                        sa.Column("data", sa.JSON(), nullable=False))
    rows_t, cols_t = _kept(ACH_ROWS, kept_rows), _kept(ACH_COLS, kept_cols)
    art_rows_t, art_cols_t = _kept(ART_ROWS, kept_rows), _kept(ART_COLS, kept_cols)

    # 1. achievement rows the previous release must not see: retired ones, and later rows of one name
    aside: list[dict] = []
    first: set[tuple[str, str, str]] = set()
    order = (achievement_new.c.canonical_id, achievement_new.c.source, achievement_new.c.norm_name,
             achievement_new.c.source_order.is_(None), achievement_new.c.source_order, achievement_new.c.id)
    for r in bind.execute(sa.select(achievement_new).order_by(*order)).mappings():
        ident = (r["canonical_id"], r["source"], r["norm_name"])
        if r["retired_at"] is not None or ident in first:
            aside.append({k: _plain(v) for k, v in r.items()})
        else:
            first.add(ident)
    for data in aside:
        texts = {t.locale: {"name": t.name, "description": t.description} for t in bind.execute(
            sa.select(text_t).where(text_t.c.achievement_id == data["id"]))}
        bind.execute(rows_t.insert().values(id=data["id"], canonical_id=data["canonical_id"],
                                            data={**data, "_texts": texts}))
        bind.execute(text_t.delete().where(text_t.c.achievement_id == data["id"]))
        bind.execute(achievement_old.delete().where(achievement_old.c.id == data["id"]))

    # 2. the new columns of the rows that stay; a curated icon under a withheld licence is set aside with them
    defaults = {"set_key": "base"}
    for r in bind.execute(sa.select(achievement_new.c.id, achievement_new.c.source,
                                    *(achievement_new.c[c] for c in ICONS),
                                    *(achievement_new.c[c] for c, _t in ACH_NEW))).mappings().all():
        data = {c: _plain(r[c]) for c, _t in ACH_NEW if r[c] is not None and r[c] != defaults.get(c)}
        if r["source"] in CURATED and r["icon_licence"] in WITHHELD and (r["icon_url"] or r["locked_icon_url"]):
            data["_icons"] = {c: r[c] for c in ICONS}
            bind.execute(achievement_new.update().where(achievement_new.c.id == r["id"])
                         .values(**{c: None for c in ICONS}))
        if data:
            bind.execute(cols_t.insert().values(id=r["id"], data=data))

    # 3. curated artwork the previous release would sign although its licence keeps it out of the CC0 feed
    withheld = sa.and_(artwork_new.c.source.in_(CURATED),
                       sa.or_(artwork_new.c.licence.in_(WITHHELD), artwork_new.c.local_only.is_(True)))
    for r in bind.execute(sa.select(artwork_new).where(withheld)).mappings().all():
        data = {k: _plain(v) for k, v in r.items()}
        data["_votes"] = [{k: _plain(v) for k, v in m.items()} for m in bind.execute(
            sa.select(vote).where(vote.c.artwork_id == r["id"])).mappings()]
        bind.execute(art_rows_t.insert().values(id=r["id"], canonical_id=r["canonical_id"], data=data))
        bind.execute(vote.delete().where(vote.c.artwork_id == r["id"]))
        bind.execute(artwork_old.delete().where(artwork_old.c.id == r["id"]))
    for r in bind.execute(sa.select(artwork_new.c.id, artwork_new.c.width, artwork_new.c.height,
                                    *(artwork_new.c[c] for c, _t in ART_NEW))).mappings():
        data = {c: _plain(r[c]) for c, _t in ART_NEW if r[c] not in (None, False)}
        if (data.get("declared_width"), data.get("declared_height")) == (r["width"], r["height"]):
            data.pop("declared_width", None), data.pop("declared_height", None)  # re-derived from width/height
        elif "declared_width" in data or "declared_height" in data:
            data["_wh"] = [r["width"], r["height"]]  # the measured size the declaration belongs to
        if data:
            bind.execute(art_cols_t.insert().values(id=r["id"], data=data))

    # 4. drop the columns and indexes, re-create the previous unique constraint
    with op.batch_alter_table("artwork") as batch:
        batch.drop_index("ix_artwork_dup")
        for column, _t in reversed(ART_NEW):
            batch.drop_column(column)
    with op.batch_alter_table("achievement") as batch:
        batch.drop_index("ix_achievement_retired_at")
        batch.drop_index("uq_achievement_ident")
        for column, _t in reversed(ACH_NEW):
            batch.drop_column(column)
        batch.create_unique_constraint("uq_achievement", ["canonical_id", "source", "norm_name"])
    for name in TABLES:
        op.rename_table(name, f"{name}_0035_kept")
