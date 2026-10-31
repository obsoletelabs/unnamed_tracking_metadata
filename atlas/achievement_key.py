"""Stable achievement identity and its wire key (CONTRACTS-v1 C2.1, decision H1).

* The **identity** of one per-source achievement row is ``"<source>:<external_id>"`` when the source gave
  an id, otherwise ``"<source>~<norm_name>"`` (community and curator lists). ``source`` is normalised with
  ``normalize.norm_namespace`` (``[a-z0-9._-]``), so it never contains ``:`` or ``~`` and the two forms
  cannot be confused.
* The **key** is ``"ak1:" + base32_lower_nopad(sha256(identity))[:26]``: always 30 characters, so it fits
  the Tracking App's 200-character ``external_id`` even when the identity itself is longer (a 50-character
  source plus a 200-character external id), and it does not spell out a hidden achievement's provider id.
* The key leaves out the canonical id, so a merge or a detach keeps it. It is unique per canonical record,
  not globally.
* The key is **stored** on the row when the row is created and then kept: renaming an entry through the
  editor (which submits the row id or key) keeps its key even though the identity derived from the new
  name would differ. ``identity`` is therefore informational once a row is renamed, never a check.
"""

from __future__ import annotations

import base64
import hashlib
import re

from .normalize import norm_namespace

PREFIX = "ak1:"
KEY_LENGTH = 30
KEY_RE = re.compile(r"ak1:[a-z2-7]{26}\Z")


def norm_name(name: str) -> str:
    """The name form achievements are grouped and matched by (the same as `facets._norm_name`)."""
    return re.sub(r"\W+", " ", name.casefold()).strip()


def identity(source: str, external_id: str, name_norm: str) -> str:
    """The plain identity of one per-source row (C2.1)."""
    ns = norm_namespace(source)
    return f"{ns}:{external_id}" if external_id else f"{ns}~{name_norm}"


def name_identity(source: str, name_norm: str) -> str:
    """The name form of the identity, used when a list repeats the same external id under two names."""
    return f"{norm_namespace(source)}~{name_norm}"


def ak1(ident: str) -> str:
    digest = hashlib.sha256(ident.encode("utf-8")).digest()
    return PREFIX + base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:26]


def is_key(value: object) -> bool:
    return isinstance(value, str) and KEY_RE.match(value) is not None


def default_key(context) -> str:  # noqa: ANN001 - SQLAlchemy default context
    """Column default for rows inserted without an explicit key (every Atlas code path sets one)."""
    p = context.get_current_parameters()
    return ak1(identity(p.get("source") or "", p.get("external_id") or "", p.get("norm_name") or ""))
