"""Catalogue quality aggregates for `/metrics` (#72): grouped counts only, never a record, title or person.

Run through the `catalog` collector, which caches the result for `ATLAS_METRICS_CACHE_SECONDS`: these are a handful
of grouped COUNT queries over the catalogue tables, not a per-record scan in Python, but on a large catalogue they
still cost more than a scrape should pay every 15 seconds. `atlas_collector_refresh_duration_seconds` measures them.

Denominators: every coverage series counts *live* records (not merged away) of that media type group, as `present`
plus `missing`; their sum is `atlas_catalog_entities{state="live"}` for the group. A field that does not apply to a
media type (achievements outside games) is not exported for it, so absence never reads as a defect. Admin-defined
media types are grouped as `custom`; their names never become label values.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .custom import BUILTIN_MEDIA_TYPES
from .models import Achievement, Artwork, Canonical, Crosswalk, FieldConflict, Proposal, Relation, ReviewItem
from .vocab import RELATION_TYPES

#: Fields measured for coverage, and the media type groups each applies to (None: all of them).
FIELDS: dict[str, tuple[str, ...] | None] = {
    "release_date": None, "artwork": None, "external_ids": None, "developer": None, "publisher": None,
    "achievements": ("game",),
}
MEDIA_GROUPS = (*BUILTIN_MEDIA_TYPES, "custom")


def media_group(media_type: str | None) -> str:
    return media_type if media_type in BUILTIN_MEDIA_TYPES else "custom"


def _grouped(rows) -> Counter[str]:
    out: Counter[str] = Counter()
    for media_type, n in rows:
        out[media_group(media_type)] += int(n or 0)
    return out


def lines(session: Session) -> list[str]:
    live_filter = Canonical.merged_into.is_(None)
    entities: Counter[tuple[str, str]] = Counter()
    for media_type, merged, n in session.execute(
            select(Canonical.media_type, Canonical.merged_into.is_not(None), func.count())
            .group_by(Canonical.media_type, Canonical.merged_into.is_not(None))):
        entities[(media_group(media_type), "merged" if merged else "live")] += n
    out = ["# TYPE atlas_catalog_entities gauge"]
    groups = sorted({g for g, _ in entities})
    for group in groups:
        for state in ("live", "merged"):
            out.append(f'atlas_catalog_entities{{media_type="{group}",state="{state}"}} {entities[(group, state)]}')

    live = {g: entities[(g, "live")] for g in groups}
    by_type = select(Canonical.media_type, func.count()).where(live_filter).group_by(Canonical.media_type)
    present: dict[str, Counter[str]] = {
        "release_date": _grouped(session.execute(by_type.where(Canonical.release_start.is_not(None)))),
        "developer": _grouped(session.execute(by_type.where(
            func.coalesce(Canonical.view["developer"].as_string(), "") != ""))),
        "publisher": _grouped(session.execute(by_type.where(
            func.coalesce(Canonical.view["publisher"].as_string(), "") != ""))),
    }
    for field, table in (("artwork", Artwork), ("external_ids", Crosswalk), ("achievements", Achievement)):
        present[field] = _grouped(session.execute(
            select(Canonical.media_type, func.count(func.distinct(table.canonical_id)))
            .join(Canonical, Canonical.id == table.canonical_id).where(live_filter)
            .group_by(Canonical.media_type)))
    out.append("# TYPE atlas_catalog_field_coverage gauge")
    for field, applies in FIELDS.items():
        for group in groups:
            if applies is not None and group not in applies:
                continue  # not applicable: not exported, so it is not counted as missing
            have = min(present[field][group], live[group])
            out.append(f'atlas_catalog_field_coverage{{media_type="{group}",field="{field}",state="present"}} {have}')
            out.append(f'atlas_catalog_field_coverage{{media_type="{group}",field="{field}",state="missing"}} '
                       f'{live[group] - have}')

    relations: Counter[tuple[str, str]] = Counter()
    for kind, ended, n in session.execute(select(Relation.type, Relation.ended_at.is_not(None), func.count())
                                          .group_by(Relation.type, Relation.ended_at.is_not(None))):
        relations[(kind if kind in RELATION_TYPES else "other", "ended" if ended else "active")] += n
    out.append("# TYPE atlas_catalog_relationships gauge")
    for (kind, state), n in sorted(relations.items()):
        out.append(f'atlas_catalog_relationships{{type="{kind}",state="{state}"}} {n}')

    definitions: Counter[str] = Counter()
    for status, retired, n in session.execute(select(Achievement.status, Achievement.retired_at.is_not(None),
                                                     func.count())
                                              .group_by(Achievement.status, Achievement.retired_at.is_not(None))):
        state = "retired" if retired else status if status in ("active", "unobtainable") else "unknown"
        definitions[state] += n
    out.append("# TYPE atlas_catalog_achievement_definitions gauge")
    out += [f'atlas_catalog_achievement_definitions{{state="{state}"}} {definitions[state]}'
            for state in ("active", "unobtainable", "unknown", "retired")]

    review = {
        "match_review": session.scalar(select(func.count()).select_from(ReviewItem)
                                       .where(ReviewItem.status == "open")) or 0,
        "proposal": session.scalar(select(func.count()).select_from(Proposal)
                                   .where(Proposal.status == "pending")) or 0,
        "field_conflict": session.scalar(select(func.count()).select_from(FieldConflict)) or 0,
    }
    out.append("# TYPE atlas_catalog_review_pending gauge")
    out += [f'atlas_catalog_review_pending{{queue="{queue}"}} {n}' for queue, n in review.items()]
    return out
