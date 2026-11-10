"""Trust presets for the owner group's local policy (SPEC-UI-POLISH-PASS §4, owner decision D1).

A preset is a named set of values for this node's own local policy (C10 §8.1). "Same owner, full trust" is for the
owner's own machines: group sign-in works without a second prompt for a second factor already given on the other
node, and a first group sign-in may create the account here (only while registration is open). It never gives
delegated administrator rights: those come only from the separate, unticked "administrator" choice, which sets this
node's ceiling for group grants to `admin` and asks for this node's own second factor (`local_mfa`); a grant must
still be issued on the grants form by the root holder, with the typed confirmation. No preset changes the scopes or
the delegation of an invite (critic §4.2: a delegation to sign grants would let the new node mint admin grants).

Local policy is never set from the group (runtime.py): each node applies the preset it chose to itself, when its
enrolment becomes active (H-UI7). C14 §3.8 adds its own rows to the same preset (D14.5) when C14-2 lands.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import runtime
from .settings import Settings

SAME_OWNER, CAUTIOUS, CUSTOM = "same_owner", "cautious", "custom"
PRESETS = (SAME_OWNER, CAUTIOUS, CUSTOM)
LABELS = {SAME_OWNER: "Same owner, full trust", CAUTIOUS: "Cautious (defaults)", CUSTOM: "Custom"}
#: The rows a preset decides (§4.2). Every other policy row (synced fields, freshness, hold timeout, denied
#: accounts) is left as it is.
KEYS = ("group_profile_sync", "group_signin", "group_signin_public", "group_auto_provision", "group_trust_remote_mfa",
        "group_delegated_requires_mfa", "group_federated_privilege", "group_max_delegated_role")
_FULL_TRUST = {"group_auto_provision": True, "group_trust_remote_mfa": True}
_WITH_ADMIN = {"group_federated_privilege": "local_mfa", "group_max_delegated_role": "admin"}
#: The rows an enrolment's preset writes when the join completes (U3 review): only those that tell the presets
#: apart. The other rows (group sign-in, the public login buttons, profile sync, local MFA for delegated rights) keep
#: whatever the administrator set, so a policy edit made between the compare step and activation is not overwritten
#: silently. "Apply" on the policy page still writes every row of §4.2.
ACTIVATION_KEYS = (*_FULL_TRUST, *_WITH_ADMIN)


def _defaults() -> dict[str, Any]:
    fields = Settings.model_fields
    return {k: fields[k].default for k in KEYS}


def values(preset: str, with_admin: bool = False, keys: tuple[str, ...] = KEYS) -> dict[str, Any]:
    """The policy values of a preset (cautious = this release's defaults), limited to `keys`."""
    if preset not in (SAME_OWNER, CAUTIOUS):
        raise ValueError(f"unknown preset {preset!r}")
    out = _defaults()
    if preset == SAME_OWNER:
        out.update(_FULL_TRUST)
        if with_admin:
            out.update(_WITH_ADMIN)
    return {k: v for k, v in out.items() if k in keys}


def kept(settings: Settings, preset: str | None, with_admin: bool = False) -> list[dict[str, Any]]:
    """The §4.2 rows an enrolment's activation leaves as the administrator set them although the preset would set
    them differently (the rows outside ACTIVATION_KEYS). Shown on the done step so a node whose profile sync or group
    sign-in is off does not silently fail to "feel like one node"."""
    rest = tuple(k for k in KEYS if k not in ACTIVATION_KEYS)
    return diff(settings, preset, with_admin, keys=rest)


def current(settings: Settings) -> dict[str, Any]:
    """Which preset the current policy equals: {"preset": name, "with_admin": bool}, or custom."""
    now = {k: getattr(settings, k) for k in KEYS}
    for preset, admin in ((SAME_OWNER, False), (SAME_OWNER, True), (CAUTIOUS, False)):
        if now == values(preset, admin):
            return {"preset": preset, "with_admin": admin, "label": LABELS[preset]}
    return {"preset": CUSTOM, "with_admin": False, "label": LABELS[CUSTOM]}


def diff(settings: Settings, preset: str | None, with_admin: bool = False,
         keys: tuple[str, ...] = KEYS) -> list[dict[str, Any]]:
    """What applying the preset changes here: [{key, label, from, to}], unchanged rows left out (§4.3). Pass
    `keys=ACTIVATION_KEYS` for what an enrolment's preset will change when the join completes."""
    if preset not in (SAME_OWNER, CAUTIOUS):
        return []
    out = []
    for key, value in values(preset, with_admin, keys).items():
        old = getattr(settings, key)
        if old != value:
            spec = runtime.BY_KEY[key]
            out.append({"key": key, "label": spec.label, "from": _shown(old), "to": _shown(value)})
    return out


def _shown(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def apply(session: Session, settings: Settings, preset: str, with_admin: bool, actor: str | None,
          cli: bool = False, keys: tuple[str, ...] = KEYS, target: str = "local") -> list[str]:
    """Write the preset to this node's local policy (the runtime settings store) and audit it. Returns the keys
    that changed. The caller refreshes the in-memory settings. `target` names what applied it in the audit line
    ("local" for the policy page or the CLI, "enrolment:<id>" on activation)."""
    from . import groups

    changed = runtime.save(session, settings, actor, values(preset, with_admin, keys))
    groups.audit(session, actor, "group_policy_preset", target,
                 {"preset": preset, "with_admin": bool(with_admin), "changed": changed}, cli)
    return changed


def activation_target(row: Any) -> str:
    return f"enrolment:{row.id}"


def apply_on_activation(session: Session, settings: Settings, row: Any, actor: str | None) -> list[str]:
    """Called when an enrolment becomes active on this side: apply the preset this side chose, if any.

    A local policy that cannot be written never breaks the enrolment protocol (U3 review): the write runs in a
    savepoint, a failure is rolled back to it and audited (`group_policy_preset_failed`), and the join still
    completes. The administrator then applies the preset by hand on the policy page."""
    from . import groups

    if row.preset not in (SAME_OWNER, CAUTIOUS):
        return []
    try:
        with session.begin_nested():
            return apply(session, settings, row.preset, bool(row.preset_admin), actor, keys=ACTIVATION_KEYS,
                         target=activation_target(row))
    except Exception as exc:  # noqa: BLE001  (any failure of the local write, a database error included)
        groups.audit(session, actor, "group_policy_preset_failed", activation_target(row),
                     {"preset": row.preset, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return []
