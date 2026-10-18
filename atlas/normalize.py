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


def norm_title(title: str | None) -> str:
    if not title:
        return ""
    base, _ = split_edition(title)
    base = _TRAILING_BRACKET.sub("", base) if base.count("(") + base.count("[") else base
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
    return " ".join(tokens)


def digit_tokens(norm: str) -> frozenset[str]:
    return frozenset(t for t in norm.split() if any(c.isdigit() for c in t))


def year_of(release_date: str | None) -> int | None:
    if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def norm_namespace(source: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", source.strip().casefold())[:50] or "unknown"
