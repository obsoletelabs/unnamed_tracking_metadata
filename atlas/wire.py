"""Wire versions and active version negotiation (CONTRACT-C16 §22, `_notes/C16-VERSION-NEGOTIATION-DESIGN.md`).

One integer names the whole Atlas-to-Atlas contract of a release: the feed handshake and content rules, the group
channel, the device link, read-API shapes, signing domains and canonical vectors. Every build before the first wire
release is **v0** (it sends `Atlas-Protocol: M.m`, or nothing, and advertises `/sync/info.protocol` without
`/sync/info.wire`). This release, Atlas 2.0.0, speaks **wire 1** and reads back to v0.

Each release declares, as code constants and never as a setting:

* `WIRE_MAX`: the newest wire it speaks;
* `WIRE_MIN`: the oldest wire it reads (0: it reads v0 upstream feeds);
* `WIRE_MIN_PEER`: the oldest *wire* peer it speaks down to (>= 1; v0 is not a version anyone negotiates).

A pair of nodes uses the **highest common version** (`negotiate`). A newer node adapts *down* and reports what it
had to omit for that peer; no overlap is a loud 409 `wire_unsupported` naming both ranges. A later release raises
`WIRE_MIN` only on purpose, announced in its release notes. There is no fixed window.

Headers: requests carry `Atlas-Wire: min=1, max=1`; negotiated responses `Atlas-Wire: use=1, min=1, max=1`. The
legacy `Atlas-Protocol` header is read only to recognise (and name) a v0 peer, never sent by a wire build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Newest wire this build speaks. Bumped at most once per release that changes the wire.
WIRE_MAX = 1
#: Oldest wire this build reads. 0 = it reads (ingests) v0 upstream feeds. Raised only deliberately (gate G-W7).
WIRE_MIN = 0
#: Oldest wire peer it speaks down to. v0 followers are refused (`peer_v0`); serving them is not built (W-3 final).
WIRE_MIN_PEER = max(WIRE_MIN, 1)
#: Whether this build reads v0 upstreams (`/sync/info.wire.reads_v0`) and serves v0 followers (never, W-3).
READS_V0 = WIRE_MIN == 0
SERVES_V0 = False

HEADER = "Atlas-Wire"
#: Read only to recognise a v0 peer; never sent by a wire build (G-W6).
LEGACY_HEADER = "Atlas-Protocol"
#: What a v0 peer that sends no header at all speaks (pre-A1 builds).
LEGACY_PROTOCOL = "1.0"
#: The frozen v0 tombstone in `/sync/info` (W-4): 1.1-1.5 builds read `min_follower_protocol` and refuse themselves
#: with their existing "upstream needs protocol >= 2.0 ... Upgrade Atlas." message. A wire build never stores,
#: compares or displays these values (design §2.3).
V0_TOMBSTONE = {"protocol": "2.0", "min_follower_protocol": "2.0"}

_HEADER_RE = re.compile(r"^\s*[a-z_]{1,16}=[0-9]{1,4}(\s*,\s*[a-z_]{1,16}=[0-9]{1,4}){0,7}\s*$")
_LEGACY_RE = re.compile(r"(\d{1,4})\.(\d{1,4})\Z")
REASONS = ("peer_too_old", "peer_too_new", "peer_v0", "peer_downgraded")
ACTIONS = ("upgrade_client", "upgrade_server", "admin_review")


@dataclass(frozen=True)
class Range:
    """What one side advertises: the newest wire it speaks and the oldest it speaks to."""

    min: int
    max: int

    def as_dict(self) -> dict[str, int]:
        return {"min": self.min, "max": self.max}

    def __str__(self) -> str:
        return f"{self.min}" if self.min == self.max else f"{self.min}-{self.max}"


@dataclass(frozen=True)
class WireProfile:
    """The wire facts of one build, read through one object (critic CR-13) so a test can run two nodes with
    different profiles in one process. The module constants are the default profile."""

    max: int = WIRE_MAX
    min: int = WIRE_MIN
    min_peer: int = WIRE_MIN_PEER
    serves_v0: bool = SERVES_V0
    since: dict[Any, int] = field(default_factory=dict)

    @property
    def reads_v0(self) -> bool:
        return self.min == 0

    @property
    def speaks(self) -> Range:
        return Range(self.min_peer, self.max)

    def advertised(self) -> dict[str, Any]:
        """`/sync/info.wire` and `/health.wire` (CR-8: `min` is the oldest wire peer, never 0)."""
        return {"min": self.min_peer, "max": self.max, "reads_v0": self.reads_v0, "serves_v0": self.serves_v0}


DEFAULT = WireProfile()


def profile(state: Any = None) -> WireProfile:
    """The profile on an app state or context (`wire_profile`), else the default one."""
    found = getattr(state, "wire_profile", None) if state is not None else None
    return found if isinstance(found, WireProfile) else DEFAULT


class InvalidWire(ValueError):
    """A malformed `Atlas-Wire` header or JSON range: 400 `invalid_wire`, never guessed at."""


class WireUnsupported(Exception):
    """No overlap between two ranges, or a v0 peer on a wire channel: 409 `wire_unsupported` (design §6)."""

    def __init__(self, mine: Range, theirs: Range | None, reason: str, *, legacy_protocol: str | None = None,
                 release: str | None = None, node: str | None = None):
        assert reason in REASONS, reason
        self.mine, self.theirs, self.reason = mine, theirs, reason
        self.legacy_protocol, self.release, self.node = legacy_protocol, release, node
        super().__init__(self.detail)

    @property
    def action(self) -> str:
        if self.reason in ("peer_too_old", "peer_v0"):
            return "upgrade_client"
        if self.reason == "peer_too_new":
            return "upgrade_server"
        return "admin_review"

    @property
    def detail(self) -> str:
        here = f"This Atlas node{f' ({self.release})' if self.release else ''} speaks wire {self.mine}."
        if self.reason == "peer_v0":
            v0 = f", protocol {self.legacy_protocol}" if self.legacy_protocol else ""
            return (f"The caller runs Atlas from before the first wire version (v0{v0}). {here} This node does not "
                    "serve v0 followers: upgrade it to Atlas 2.0.0 or newer (its data and sync cursor are kept).")
        if self.reason == "peer_downgraded":
            return (f"{here} The peer was seen speaking wire before and now looks older (a downgraded build or "
                    "interception). An administrator has to accept the downgrade.")
        theirs = f"wire {self.theirs}" if self.theirs else "no wire version"
        if self.reason == "peer_too_old":
            return (f"{here} The caller speaks {theirs}. Upgrade the caller to an Atlas release that speaks wire "
                    f"{self.mine.min} or newer; its data and sync cursor are kept.")
        return (f"{here} The caller speaks {theirs}. Upgrade this node to an Atlas release that speaks wire "
                f"{self.theirs.min if self.theirs else '?'} or newer.")

    def body(self) -> dict[str, Any]:
        server: dict[str, Any] = {"wire": self.mine.as_dict()}
        if self.release:
            server["release"] = self.release
        if self.node:
            server["node"] = self.node
        client: dict[str, Any] = ({"legacy_protocol": self.legacy_protocol} if self.reason == "peer_v0"
                                  else {"wire": self.theirs.as_dict() if self.theirs else None})
        return {"code": "wire_unsupported", "reason": self.reason, "detail": self.detail, "server": server,
                "client": client, "action": self.action}


# ---- header grammar --------------------------------------------------------------------------------------

def parse_header(value: str | None) -> dict[str, int] | None:
    """`min=1, max=3` -> {"min": 1, "max": 3}; None when absent. Raises InvalidWire on a malformed value, a
    duplicate key, a missing `min`/`max`, `min < 1`, `min > max` or a value above 9999. Unknown keys are kept
    (and ignored by callers)."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200 or not _HEADER_RE.fullmatch(value):
        raise InvalidWire("Atlas-Wire must look like `min=1, max=1`")
    out: dict[str, int] = {}
    for part in value.split(","):
        key, _, number = part.strip().partition("=")
        if key in out:
            raise InvalidWire(f"Atlas-Wire names {key!r} twice")
        out[key] = int(number)
    if "min" not in out or "max" not in out:
        raise InvalidWire("Atlas-Wire needs both min and max")
    check_range(out["min"], out["max"])
    return out


def check_range(lo: Any, hi: Any) -> Range:
    if type(lo) is not int or type(hi) is not int or not 1 <= lo <= hi <= 9999:
        raise InvalidWire("a wire range is two integers 1 <= min <= max <= 9999")
    return Range(lo, hi)


def range_of(obj: Any) -> Range:
    """A JSON `{"min", "max"}` (the `/sync/info.wire` and group hello form)."""
    if not isinstance(obj, dict):
        raise InvalidWire("a wire range is an object {min, max}")
    return check_range(obj.get("min"), obj.get("max"))


def request_header(prof: WireProfile = DEFAULT) -> str:
    return f"min={prof.min_peer}, max={prof.max}"


def response_header(use: int, prof: WireProfile = DEFAULT) -> str:
    return f"use={use}, min={prof.min_peer}, max={prof.max}"


def legacy_protocol(value: Any) -> str | None:
    """The `M.m` a v0 peer sent in `Atlas-Protocol`, for display in the refusal only (never stored as a wire)."""
    return value if isinstance(value, str) and _LEGACY_RE.fullmatch(value) else None


# ---- negotiation ------------------------------------------------------------------------------------------

def negotiate(mine: Range, theirs: Range) -> int:
    """The highest common version, or WireUnsupported. `mine.min` is this side's oldest *wire* peer."""
    lo, hi = max(mine.min, theirs.min), min(mine.max, theirs.max)
    if lo > hi:
        raise WireUnsupported(mine, theirs, "peer_too_old" if theirs.max < mine.min else "peer_too_new")
    return hi


def negotiate_request(header: str | None, *, legacy: str | None = None, prof: WireProfile = DEFAULT,
                      peer_channel: bool = True) -> int:
    """Server side of one request. No `Atlas-Wire`: on a peer channel that is a v0 caller (`peer_v0`); on the read
    API it is served at `WIRE_MAX` (owner: the highest version by default). Raises InvalidWire on a malformed
    header (400 `invalid_wire`, after authentication) and WireUnsupported on no overlap."""
    theirs = parse_header(header)
    if theirs is None:
        if peer_channel:
            raise WireUnsupported(prof.speaks, None, "peer_v0", legacy_protocol=legacy_protocol(legacy)
                                  or (None if legacy else LEGACY_PROTOCOL))
        return prof.max
    return negotiate(prof.speaks, Range(theirs["min"], theirs["max"]))


def upstream_wire(info: dict[str, Any], prof: WireProfile = DEFAULT) -> tuple[int, Range | None]:
    """Follower side, from an upstream's `/sync/info`. Returns `(use, range)`; `(0, None)` for a v0 upstream (no
    `wire`), which this build reads only while `WIRE_MIN == 0`. Raises WireUnsupported (`peer_too_new` from this
    follower's point of view when the upstream only speaks newer wires than this build) or InvalidWire."""
    raw = info.get("wire")
    if raw is None:
        if not prof.reads_v0:
            raise WireUnsupported(prof.speaks, None, "peer_too_old")
        return 0, None
    theirs = range_of(raw)
    lo, hi = max(prof.min_peer, theirs.min), min(prof.max, theirs.max)
    if lo > hi:
        # this follower is too old for the upstream (or the upstream too old for this follower)
        raise WireUnsupported(theirs, prof.speaks, "peer_too_old" if prof.max < theirs.min else "peer_too_new")
    return hi, theirs


def is_v0_info(info: dict[str, Any]) -> bool:
    return isinstance(info, dict) and info.get("wire") is None


# ---- adapters at the edge (design §5.1) -----------------------------------------------------------------------

@dataclass
class AdapterRegistry:
    """Adjacent-step adapters per schema id, plus `no_wire_change` declarations. Wire 1 has no steps; the
    completeness check and composition run on a synthetic registry in the tests."""

    steps: dict[tuple[str, int, int, str], Any] = field(default_factory=dict)
    unchanged: dict[tuple[str, int], str] = field(default_factory=dict)
    schema_ids: set[str] = field(default_factory=set)

    def adapter(self, schema_id: str, frm: int, to: int, direction: str = "response"):
        if abs(frm - to) != 1:
            raise ValueError("adapters are adjacent steps (N <-> N-1) only")
        if direction not in ("request", "response"):
            raise ValueError("direction is request or response")

        def register(fn):
            self.steps[(schema_id, frm, to, direction)] = fn
            self.schema_ids.add(schema_id)
            return fn
        return register

    def no_wire_change(self, schema_id: str, at: int, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a no_wire_change declaration needs a reason")
        self.unchanged[(schema_id, at)] = reason
        self.schema_ids.add(schema_id)

    def missing(self, prof: WireProfile = DEFAULT) -> list[str]:
        """Every schema id x step in (min_peer, max] without an adapter (both directions) or a declaration."""
        out = []
        for sid in sorted(self.schema_ids):
            for n in range(prof.min_peer + 1, prof.max + 1):
                if (sid, n) in self.unchanged:
                    continue
                for direction, frm, to in (("response", n, n - 1), ("request", n - 1, n)):
                    if (sid, frm, to, direction) not in self.steps:
                        out.append(f"{sid} {direction} {frm}->{to}")
        return out

    def check(self, prof: WireProfile = DEFAULT) -> None:
        gaps = self.missing(prof)
        if gaps:
            raise RuntimeError("wire adapter registry is incomplete: " + "; ".join(gaps[:20]))

    def down(self, schema_id: str, body: dict[str, Any], frm: int, to: int) -> tuple[dict[str, Any], list[str]]:
        """Compose response adapters frm -> to (frm >= to). Returns the adapted body and what was omitted."""
        omitted: list[str] = []
        for n in range(frm, to, -1):
            if (schema_id, n) in self.unchanged:
                continue
            body = self.steps[(schema_id, n, n - 1, "response")](body, omitted)
        return body, omitted

    def up(self, schema_id: str, body: dict[str, Any], frm: int, to: int) -> dict[str, Any]:
        for n in range(frm, to):
            if (schema_id, n + 1) in self.unchanged:
                continue
            body = self.steps[(schema_id, n, n + 1, "request")](body)
        return body


#: The registry of this build. Empty: wire 1 is the first wire version, so nothing adapts yet.
REGISTRY = AdapterRegistry()
REGISTRY.check()


# ---- start-up (design §4.7, §2.4) -------------------------------------------------------------------------------

HIGH_WATER = "wire_high_water"
#: Environment variables and stored settings of the v0 emission gates (C16 §22): ignored, logged once at start.
IGNORED_ENV = ("ATLAS_EMIT_PROTOCOL", "ATLAS_MIN_FOLLOWER_PROTOCOL", "ATLAS_FEDERATION_BRIDGE_RECORD_OBS",
               "ATLAS_FEDERATION_BRIDGE_MEDIA_TYPES")
IGNORED_SETTINGS = ("emit_protocol", "min_follower_protocol", "federation_bridge_record_obs",
                    "federation_bridge_media_types")


class WireHighWater(RuntimeError):
    """This database was used by a build with a higher `WIRE_MAX` (design §4.7, critic CR-7)."""


def check_high_water(session: Any, prof: WireProfile = DEFAULT, *, restoring: bool = False) -> int:
    """Refuse to start a build whose `WIRE_MAX` is below the highest one that ever ran on this database (it would
    re-classify and park its own feed); raise the mark otherwise. `restoring`: `atlas restore` may go back."""
    from .models import NodeState

    row = session.get(NodeState, HIGH_WATER)
    seen = int(row.value) if row is not None and str(row.value).isdigit() else 0
    if seen > prof.max and not restoring:
        raise WireHighWater(f"this database was used by a wire-{seen} build and this build speaks wire {prof.max} at "
                            "most: restore the pre-upgrade backup or run that release")
    if row is None:
        session.add(NodeState(key=HIGH_WATER, value=str(prof.max)))
    elif seen < prof.max or restoring:
        row.value = str(prof.max)
    session.flush()
    return max(seen, prof.max)


def log_ignored(session: Any, environ: dict[str, str], log: Any) -> list[str]:
    """One log line per ignored v0 version setting (design §2.4): the version is chosen per request now."""
    from .models import Setting

    found = [name for name in IGNORED_ENV if environ.get(name)]
    found += [f"setting.{key}" for key in IGNORED_SETTINGS if session.get(Setting, key) is not None]
    for name in found:
        log.warning("ignored: %s (the version is chosen per request since Atlas 2.0, wire 1)", name)
    return found
