"""Title normalisation for matching. Pure functions, no I/O."""

from __future__ import annotations

import re
import unicodedata

# single letters (i, v, x) are left alone: "Mega Man X" is not "Mega Man 10"
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_EDITION_WORDS = (
    "game of the year", "goty", "definitive", "complete", "deluxe", "ultimate", "enhanced", "remastered",
    "remaster", "director's cut", "directors cut", "gold", "standard", "collector's", "anniversary",
    "legendary", "special", "premium", "digital",
)
_EDITION_RE = re.compile(
    r"[\s:\-–—]*[\(\[]?\b(?:(?:" + "|".join(re.escape(w) for w in _EDITION_WORDS) + r")\s*)+edition\b[\)\]]?\s*$",
    re.IGNORECASE,
)
_TRAILING_BRACKET = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)


def split_edition(title: str) -> tuple[str, str | None]:
    """'Hades: Definitive Edition' -> ('Hades', 'Definitive Edition')."""
    match = _EDITION_RE.search(title)
    if match:
        edition = re.sub(r"^[\s:\-–—(\[]+|[\s)\]]+$", "", match.group(0))
        return title[: match.start()].strip(), edition or None
    return title.strip(), None


#: Media types whose titles keep their "... Edition" words (CONTRACT-C15 §2.5): "The Second Edition" is a real book
#: title, so stripping it would make two different works one key. Their trailing brackets are kept for the same reason.
KEEPS_EDITION_WORDS = frozenset({"book"})


def norm_title(title: str | None, media_type: str | None = None) -> str:
    """The matching key of a title. Type-aware (C15 §2.5): for a book the edition words and a trailing bracket stay
    part of the title; for every other type (and an unknown one) they are stripped as before."""
    if not title:
        return ""
    keep = media_type in KEEPS_EDITION_WORDS
    base = title.strip() if keep else split_edition(title)[0]
    if not keep and base.count("(") + base.count("["):
        base = _TRAILING_BRACKET.sub("", base)
    base = re.sub(r"[™®©]", "", base)
    text = unicodedata.normalize("NFKD", base)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("&", " and ")
    text = text.replace("_", " ")
    text = _NONWORD.sub(" ", text)
    tokens = text.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    tokens = [_ROMAN.get(t, t) if i > 0 else t for i, t in enumerate(tokens)]
    return " ".join(tokens)[:400]


def digit_tokens(norm: str) -> frozenset[str]:
    return frozenset(t for t in norm.split() if any(c.isdigit() for c in t))


def year_of(release_date: str | None) -> int | None:
    if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def norm_namespace(source: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", source.strip().casefold())[:50] or "unknown"


# ---- ISBN (CONTRACT-C15 §2.5) ------------------------------------------------------------------------------

_ISBN_SEPARATORS = re.compile(r"[\s\-‐-―]")  # spaces, hyphens and the Unicode dashes
_ISBN10 = re.compile(r"\d{9}[\dX]\Z")
_ISBN13 = re.compile(r"97[89]\d{10}\Z")


def _isbn10_check(first9: str) -> str:
    total = sum((10 - i) * int(c) for i, c in enumerate(first9))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


def _isbn13_check(first12: str) -> str:
    total = sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(first12))
    return str((10 - total % 10) % 10)


def isbn13(value: object) -> str | None:
    """The ISBN-13 of an ISBN-10 or ISBN-13, or None when the value is not a valid ISBN (never guessed).

    Spaces and hyphens are stripped and `x` upper-cased; the check digit must be right. An ISBN-10 becomes `978` +
    its nine digits + a recomputed check digit. Only the `978` and `979` prefixes are ISBNs."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    text = _ISBN_SEPARATORS.sub("", value).upper()
    if _ISBN10.match(text):
        if _isbn10_check(text[:9]) != text[9]:
            return None
        body = "978" + text[:9]
        return body + _isbn13_check(body)
    if _ISBN13.match(text) and _isbn13_check(text[:12]) == text[12]:
        return text
    return None


def isbn10(value: object) -> str | None:
    """The ISBN-10 of a valid ISBN (10 or 13 digits), or None: a `979` ISBN has no ISBN-10."""
    thirteen = isbn13(value)
    if thirteen is None or not thirteen.startswith("978"):
        return None
    return thirteen[3:12] + _isbn10_check(thirteen[3:12])


def looks_like_isbn(value: object) -> bool:
    """True when `value` is a valid ISBN-10 or ISBN-13 (hyphens and spaces allowed)."""
    return isbn13(value) is not None
