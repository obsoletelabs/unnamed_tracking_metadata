"""Books as a built-in media type (CONTRACT-C15 §2, A21): ISBN crosswalk rows, book lock values and the derived
`book_creator` rows.

A book is a work-level record (`media_type = "book"`); its editions are the `editions[]` view (H11), never records.
What a book observation adds travels in `observation.payload.ext.book` (`contract.BookInfoV1`), registered under the
unified release version (`feed_schema.UNIFIED`, capability `book-v1`). The merge rules live in `atlas/merge.py`
(`BOOK_LOCKS`, the ISBN-first `editions[]` view, `developer` derived from the first author)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .contract import MAX_AUTHORS, MAX_PAGES, BookAuthorV1
from .merge import book_of, source_class
from .models import BookCreator, Canonical, Crosswalk, Observation
from .normalize import isbn13

#: The media type key of the built-in book type.
BOOK = "book"
#: C15 §2.5: the crosswalk namespace of ISBN-13s. Each one is a variant key (`edition:<norm edition name>`), so many
#: ISBNs point at one work. It is a crosswalk namespace, never a source: it has no trust entry and is never fetched.
ISBN_NAMESPACE = "isbn"
#: The review reason when one ISBN is claimed by two works (`ReviewItem.kind = id_conflict`). Never auto-merged:
#: publishers reuse ISBNs, and an omnibus ISBN legitimately names a `collection`.
ISBN_CONFLICT = "isbn_conflict"
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{1,8})?\Z")
_NONWORD = re.compile(r"[^\w]+", re.UNICODE)


# ---- ISBN crosswalk rows (C15 §2.5) ------------------------------------------------------------------------

def _counts(obs: Observation) -> bool:
    """Whether an observation's edition ISBNs are registered: a provider's (`metadata`) or a curator's. A host or
    plugin report is a user's claim about their copy; it may match by ISBN but never registers one."""
    return obs.source_kind == "metadata" or source_class(obs.source) == "curator"


def edition_variant(entry: dict[str, Any]) -> str:
    from .merge import _edition_name

    name = " ".join(_edition_name(entry).casefold().split())[:100]
    return f"edition:{name}" if name else "edition:"


def wanted_isbns(session: Session, cid: str) -> dict[str, str]:
    """ISBN-13 -> variant for every edition of the record's current provider or curator observations."""
    out: dict[str, str] = {}
    for obs in session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                         Observation.is_current.is_(True))
                               .order_by(Observation.id)):
        if not _counts(obs):
            continue
        for entry in book_of(obs.payload or {}).get("editions") or []:
            isbn = isbn13(entry.get("isbn_13")) if isinstance(entry, dict) else None
            if isbn is not None:
                out.setdefault(isbn, edition_variant(entry))
    return out


def sync_isbn_keys(session: Session, settings: Any, cid: str) -> list[str]:
    """Make the record's `isbn` edition rows match its current observations: one row per ISBN-13 of an edition of a
    current provider or curator observation; rows of a superseded observation are removed. An ISBN another live work
    already holds is not taken: it opens (or refreshes) an `id_conflict` review with reason `isbn_conflict` and never
    merges. Returns the records that hold a conflicting ISBN. Work keys (`variant = ''`, from `provider_ids`) are left
    alone: they belong to the observation's own key registration."""
    from . import matching
    from .service import follow

    row = session.get(Canonical, cid)
    if row is None or row.merged_into is not None:
        return []
    session.flush()
    wanted = wanted_isbns(session, cid)
    for held in session.scalars(select(Crosswalk).where(Crosswalk.canonical_id == cid,
                                                        Crosswalk.namespace == ISBN_NAMESPACE,
                                                        Crosswalk.variant.like("edition:%"))).all():
        if held.external_id not in wanted:
            session.delete(held)
    session.flush()
    conflicts: dict[str, list[str]] = {}
    for isbn, variant in wanted.items():
        existing = session.get(Crosswalk, (ISBN_NAMESPACE, isbn))
        if existing is None:
            session.add(Crosswalk(namespace=ISBN_NAMESPACE, external_id=isbn, canonical_id=cid, variant=variant))
            continue
        other = follow(session, existing.canonical_id)
        if other != cid:
            conflicts.setdefault(other, []).append(isbn)
    session.flush()
    for other, isbns in sorted(conflicts.items()):
        matching.open_review(session, settings, "id_conflict", cid, [(other, 1.0)], 1.0, reason=ISBN_CONFLICT,
                             evidence=[matching.ev(ISBN_CONFLICT, [f"isbn:{i}" for i in sorted(isbns)[:5]], 0.0)],
                             tier="id")
    return sorted(conflicts)


# ---- lock values (C15 §2.3: BOOK_LOCKS) --------------------------------------------------------------------

def clean_lock(field: str, value: Any) -> Any:
    """A book lock value held to the `ext.book` rules. Raises ValueError with a message for the curator."""
    if field == "authors":
        if not isinstance(value, list) or not 0 < len(value) <= MAX_AUTHORS:
            raise ValueError(f"authors needs a list of 1-{MAX_AUTHORS} authors")
        try:
            return [BookAuthorV1.model_validate(a).model_dump(mode="json", exclude_none=True) for a in value]
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise ValueError(f"authors: {where + ': ' if where else ''}{first.get('msg', 'invalid')}") from None
    if field == "subtitle":
        if not isinstance(value, str):
            raise ValueError("subtitle needs a string")
        cleaned = " ".join("".join(c if c.isprintable() or c == " " else " " for c in value).split())
        if not 0 < len(cleaned) <= 300:
            raise ValueError("subtitle needs 1-300 printable characters")
        return cleaned
    if field == "page_count":
        if type(value) is not int or not 1 <= value <= MAX_PAGES:
            raise ValueError(f"page_count needs a whole number from 1 to {MAX_PAGES}")
        return value
    if field == "original_language":
        if not isinstance(value, str) or not _LANGUAGE.match(value):
            raise ValueError("original_language needs a language code such as 'en' or 'pt-BR'")
        return value
    raise ValueError(f"{field} is not a book field")


# ---- derived creators (C15 §5) -----------------------------------------------------------------------------

def norm_name(name: str) -> str:
    """A person's name for prefix search: casefold, without diacritics, punctuation as spaces."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(_NONWORD.sub(" ", text).split())[:200]


def rebuild_creators(session: Session, row: Canonical) -> None:
    """Replace the record's `book_creator` rows from its view (at most 30, in order). Only live book records have
    rows; a merged or retyped record loses them."""
    session.execute(delete(BookCreator).where(BookCreator.canonical_id == row.id))
    if row.media_type != BOOK or row.merged_into is not None:
        return
    authors = (row.view or {}).get("authors")
    if not isinstance(authors, list):
        return
    for position, author in enumerate(authors[:MAX_AUTHORS]):
        if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
            continue
        session.add(BookCreator(canonical_id=row.id, position=position, name=author["name"][:200],
                                norm_name=norm_name(author["name"]), role=str(author.get("role") or "author")[:16]))


# ---- the record page (C15 §5, A23) -------------------------------------------------------------------------

#: How the byline names each creator role, in the order the groups are shown ("by A and B; translated by C").
ROLE_PHRASES = (("author", "by"), ("editor", "edited by"), ("translator", "translated by"),
                ("illustrator", "illustrated by"), ("narrator", "narrated by"), ("contributor", "with"))
#: The books' data sources this page credits, with the link to the source's own page for the record.
DATA_SOURCES = {"openlibrary": ("Open Library", "https://openlibrary.org/works/{id}")}
#: Display names of the language codes books carry most (ISO 639-1 and the MARC/639-2 codes Open Library uses).
#: An unlisted code is shown as it is stored.
LANGUAGE_NAMES = {
    "ar": "Arabic", "ara": "Arabic", "cs": "Czech", "ces": "Czech", "cze": "Czech", "cy": "Welsh", "cym": "Welsh",
    "wel": "Welsh", "da": "Danish", "dan": "Danish", "de": "German", "deu": "German", "ger": "German",
    "el": "Greek", "ell": "Greek", "gre": "Greek", "en": "English", "eng": "English", "es": "Spanish",
    "spa": "Spanish", "fi": "Finnish", "fin": "Finnish", "fr": "French", "fra": "French", "fre": "French",
    "ga": "Irish", "gle": "Irish", "he": "Hebrew", "heb": "Hebrew", "hi": "Hindi", "hin": "Hindi",
    "hu": "Hungarian", "hun": "Hungarian", "is": "Icelandic", "ice": "Icelandic", "isl": "Icelandic",
    "it": "Italian", "ita": "Italian", "ja": "Japanese", "jpn": "Japanese", "ko": "Korean", "kor": "Korean",
    "la": "Latin", "lat": "Latin", "nl": "Dutch", "nld": "Dutch", "dut": "Dutch", "no": "Norwegian",
    "nor": "Norwegian", "nb": "Norwegian Bokmål", "nob": "Norwegian Bokmål", "pl": "Polish", "pol": "Polish",
    "pt": "Portuguese", "por": "Portuguese", "ru": "Russian", "rus": "Russian", "sv": "Swedish", "swe": "Swedish",
    "tr": "Turkish", "tur": "Turkish", "uk": "Ukrainian", "ukr": "Ukrainian", "zh": "Chinese", "zho": "Chinese",
    "chi": "Chinese",
}


def language_name(code: Any) -> str:
    """A language code as a name ("en", "eng" -> "English"; "pt-BR" -> "Portuguese (BR)"); unknown codes unchanged."""
    if not isinstance(code, str) or not code:
        return ""
    base, _, region = code.partition("-")
    name = LANGUAGE_NAMES.get(base.casefold())
    if name is None:
        return code
    return f"{name} ({region})" if region else name


def byline(authors: Any) -> list[dict[str, Any]]:
    """The view's ordered creators as byline groups: [{"phrase": "by", "names": [...]}, ...]. Each group keeps the
    list's order; an unknown role reads as "with"."""
    groups: dict[str, list[str]] = {}
    for a in authors if isinstance(authors, list) else []:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip():
            role = a.get("role") if a.get("role") in dict(ROLE_PHRASES) else "contributor"
            groups.setdefault(role, []).append(a["name"].strip())
    return [{"phrase": phrase, "role": role, "names": groups[role]} for role, phrase in ROLE_PHRASES if role in groups]


def attribution(view: dict[str, Any]) -> list[dict[str, str]]:
    """C15 §3.2: "Book data from Open Library" with a link to the work, for each book source whose observation the
    record holds (`view.sources`) and whose id it knows."""
    out = []
    ids = view.get("provider_ids") or {}
    for source, (label, link) in DATA_SOURCES.items():
        if source in (view.get("sources") or []):
            ident = ids.get(source) if isinstance(ids, dict) else None
            out.append({"source": source, "label": label,
                        "url": link.format(id=ident) if isinstance(ident, str) and ident else ""})
    return out


def series_of(rel: dict[str, Any]) -> list[dict[str, Any]]:
    """The series a record belongs to (`series_member` relations, C6/C15 §2.4) with its position in each."""
    return [r for r in rel.get("members_of") or [] if r.get("type") == "series_member"]


def series_hint(session: Session, cid: str) -> dict[str, str]:
    """The `ext.book.series` hint of the record's current observations (§2.4: a hint only, never a relation), the
    most recent first: {"name", "position"} or {}."""
    for obs in session.scalars(select(Observation).where(Observation.canonical_id == cid,
                                                         Observation.is_current.is_(True))
                               .order_by(Observation.observed_at.desc(), Observation.id.desc()).limit(50)):
        hint = book_of(obs.payload or {}).get("series")
        if isinstance(hint, dict) and isinstance(hint.get("name"), str) and hint["name"].strip():
            pos = hint.get("position")
            return {"name": hint["name"].strip(), "position": pos if isinstance(pos, str) else ""}
    return {}
