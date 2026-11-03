"""partial and regional release dates: first-release columns and view keys (CONTRACTS-v1 C5.2)

Revision ID: 0031
Revises: 0030

`canonical` gains the first release as a period, so year-only and month-only dates sort and filter in SQL:
`release_start` and `release_end` (the first and last day of the period) and `release_precision`
(`year|month|day`). NULL means unknown.

Backfill (existing data keeps its meaning): every record whose view has a `release_date` gets
`view.first_release = {date, precision}` and `view.release_year`, `explain.first_release`, and the three
columns. Observations only ever stored day dates, so the precision is `day` and `release_date` stays exactly
as it was. The one exception is a curator *lock* on `release_date`, which earlier releases accepted as any
text: a lock value of `"2011"` or `"2011-04"` becomes a year or month first release and leaves the day-only
`release_date` key (the lock row itself is unchanged); a lock value that is not a date at all is left in
the view untouched (and the merge keeps showing it there). `year` is set from the first release. A day
keeps any year (a pre-1950 film is a day too); only partial values use the C5.1 range 1950-2100.

Nothing else is rebuilt here: `atlas dates rebuild` re-derives every view with the new hierarchical vote
(resumable). Observations and locks are never modified.

Downgrade restores what the previous release derives: the new view and explain keys are removed, a record
whose `release_date` was moved to a partial `first_release` gets it back from its lock, `year` is recomputed
from `release_date`, and the columns are dropped. Observations that carry `ext.release` and `releases`
locks stay; the previous release ignores both (its merge reads named fields only). Records whose first release
came from `ext.release` votes or a `releases` lock therefore lose `first_release`/`releases` while downgraded; the
downgrade leaves `node_state["view_rebuild_pending"]`, and after upgrading again the application re-derives every
view (`atlas.dates_rebuild.finish_pending`, run by `atlas migrate` and at start until done), so upgrading again
brings them back without `atlas dates rebuild`.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date

import sqlalchemy as sa
from alembic import op

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None

BATCH = 500
_PARTIAL = re.compile(r"\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?\Z", re.ASCII)  # ASCII digits only

canonical = sa.table(
    "canonical",
    sa.column("id", sa.String), sa.column("view", sa.JSON), sa.column("explain", sa.JSON),
    sa.column("year", sa.Integer), sa.column("release_start", sa.Date), sa.column("release_end", sa.Date),
    sa.column("release_precision", sa.String),
)
lock = sa.table("field_lock", sa.column("canonical_id", sa.String), sa.column("field", sa.String),
                sa.column("value", sa.JSON))


#: Phase 4 defect D3: a re-upgrade re-derives what this downgrade removed (`atlas.dates_rebuild.finish_pending`
#: reads this key; spelled out here because a migration never imports application code that may change later).
REBUILD_KEY = "view_rebuild_pending"


def _ask_for_rebuild(bind) -> None:
    """Leave the node a note: once the schema is current again, the application re-derives every view."""
    state = sa.table("node_state", sa.column("key", sa.String), sa.column("value", sa.Text))
    bind.execute(state.delete().where(state.c.key == REBUILD_KEY))
    bind.execute(state.insert().values(key=REBUILD_KEY, value="pending"))


def _json(value):
    """SQLite may hand JSON back as text when read through a lightweight table."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _period(value):
    """(precision, start, end) for a PartialDate, else None. A year or a month must be in the C5.1 range
    1950-2100; a day is the legacy `release_date` and keeps any year a date can hold (a 1937 film keeps its
    date). Self-contained: a migration never imports application code that may change later."""
    if not isinstance(value, str) or not _PARTIAL.fullmatch(value):
        return None
    y = int(value[:4])
    if len(value) != 10 and not 1950 <= y <= 2100:
        return None
    if len(value) == 4:
        return "year", date(y, 1, 1), date(y, 12, 31)
    m = int(value[5:7])
    if len(value) == 7:
        return "month", date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return "day", day, day


def _batches(bind):
    last = ""
    while True:
        rows = bind.execute(sa.select(canonical.c.id, canonical.c.view, canonical.c.explain)
                            .where(canonical.c.id > last).order_by(canonical.c.id).limit(BATCH)).all()
        if not rows:
            return
        yield rows
        last = rows[-1].id


def upgrade() -> None:
    with op.batch_alter_table("canonical") as batch:
        batch.add_column(sa.Column("release_start", sa.Date(), nullable=True))
        batch.add_column(sa.Column("release_end", sa.Date(), nullable=True))
        batch.add_column(sa.Column("release_precision", sa.String(5), nullable=True))
    op.create_index("ix_canonical_release_start", "canonical", ["release_start"])

    bind = op.get_bind()
    for rows in _batches(bind):
        for row in rows:
            view, explain = dict(_json(row.view)), dict(_json(row.explain))
            got = _period(view.get("release_date"))
            if got is None:
                continue  # no date, or a value that is not a date: left exactly as it was
            precision, start, end = got
            value = view["release_date"]
            view["first_release"] = {"date": value, "precision": precision}
            view["release_year"] = start.year
            if "release_date" in explain:
                explain["first_release"] = explain["release_date"]
            if precision != "day":  # only a lock can hold this: release_date stays day-only (C5.2)
                view.pop("release_date")
                explain.pop("release_date", None)
            bind.execute(canonical.update().where(canonical.c.id == row.id).values(
                view=view, explain=explain, year=start.year, release_start=start, release_end=end,
                release_precision=precision))


def downgrade() -> None:
    bind = op.get_bind()
    locked = {r.canonical_id: r.value for r in bind.execute(
        sa.select(lock.c.canonical_id, lock.c.value).where(lock.c.field == "release_date"))}
    for rows in _batches(bind):
        for row in rows:
            view, explain = dict(_json(row.view)), dict(_json(row.explain))
            if not any(k in view for k in ("first_release", "release_year", "releases")) and \
                    not any(k in explain for k in ("first_release", "releases")):
                continue
            for key in ("first_release", "release_year", "releases"):
                view.pop(key, None)
            for key in ("first_release", "releases"):
                explain.pop(key, None)
            value = locked.get(row.id)
            if isinstance(value, str) and value and "release_date" not in view:
                view["release_date"] = value  # the previous release shows a locked value as it is
                explain["release_date"] = {"strategy": "lock"}
            rd = view.get("release_date")
            year = int(rd[:4]) if isinstance(rd, str) and len(rd) >= 4 and rd[:4].isdigit() else None
            bind.execute(canonical.update().where(canonical.c.id == row.id).values(
                view=view, explain=explain, year=year))
    _ask_for_rebuild(bind)
    op.drop_index("ix_canonical_release_start", table_name="canonical")
    with op.batch_alter_table("canonical") as batch:
        batch.drop_column("release_precision")
        batch.drop_column("release_end")
        batch.drop_column("release_start")
