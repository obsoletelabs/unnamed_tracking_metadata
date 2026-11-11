"""Trust presets for the owner group's local policy (SPEC-UI-POLISH-PASS §4, owner decision D1).

A preset is a named set of values for this node's own local policy (C10 §8.1). "Same owner, full trust" is for the
owner's own machines: group sign-in works without a second prompt for a second factor already given on the other
node, and a first group sign-in may create the account here (only while registration is open). It never gives
delegated administrator rights: those come only from the separate, unticked "administrator" choice, which sets this
node's ceiling for group grants to `admin` and asks for this node's own second factor (`local_mfa`); a grant must
still be issued on the grants form by the root holder, with the typed confirmation. No preset changes the scopes or
the delegation of an invite (critic §4.2: a delegation to sign grants would let the new node mint admin grants).

Local policy is never set from the group (runtime.py): each node applies the preset it chose to itself, when its
enrolment becomes active (H-UI7). C14 §3.8 adds its own rows to the same preset (D14.5): with "Same owner, full
trust" the node asks and answers its other nodes with group data, shares provider data with them (H59), looks
things up for them (H60), accepts what they already applied (H61), asks them for images before a third-party
origin (H62), shows group data to visitors as the partial public projection (H64, D1) and shares contribution
state (H67). None of these rows gives any right: assist ops only read, and what they bring stays `group`-scoped.
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
        "group_delegated_requires_mfa", "group_federated_privilege", "group_max_delegated_role",
        # CONTRACT-C14 §3.8 (C14-2)
        "group_assist_ask", "group_assist_answer", "group_assist_share_provider_data", "group_assist_lookups",
        "group_assist_lookup_reserve", "group_assist_auto_adopt", "group_assist_group_before_origin",
        "group_data_public_pages", "group_share_contribution_state",
        # C14-4 (super group)
        "group_super")
#: C14 §3.8: the sharing rows of the full-trust preset (H59-H62, H64, H67). `group_assist_lookup_reserve`,
#: `group_assist_ask` and `group_share_contribution_state` keep their defaults (0.5, on, on).
ASSIST_FULL_TRUST = {"group_assist_answer": "group", "group_assist_share_provider_data": "fields+media+achievements",
                     "group_assist_lookups": "answer+ask", "group_assist_auto_adopt": True,
                     "group_assist_group_before_origin": True, "group_data_public_pages": True}
#: C14-4: "Same owner, full trust" makes the group one node for its users (owner request 2026-11-10).
SUPER_FULL_TRUST = {"group_super": True}
_FULL_TRUST = {"group_auto_provision": True, "group_trust_remote_mfa": True, **ASSIST_FULL_TRUST, **SUPER_FULL_TRUST}
_WITH_ADMIN = {"group_federated_privilege": "local_mfa", "group_max_delegated_role": "admin"}
#: The rows an enrolment's preset writes when the join completes (U3 review): the rows that tell the presets apart.
#: The rows left out here (the public login buttons, local MFA for delegated rights) keep whatever the administrator
#: set, so a policy edit made between the compare step and activation is not overwritten silently; the compare step
#: lists exactly the rows that will change. "Apply" on the policy page still writes every row of §4.2.
ACTIVATION_KEYS = (*_FULL_TRUST, *_WITH_ADMIN)
#: "Same owner, full trust" also writes the two rows that make the group feel like one node (§4.2/§4.3 and the
#: polish addendum, U3 second review): profile/preference sync and group sign-in. Without them a node whose
#: administrator had turned either off would join "with full trust" and still not behave as one node.
ONE_NODE_KEYS = ("group_profile_sync", "group_signin")


def activation_keys(preset: str | None) -> tuple[str, ...]:
    """The rows an enrolment that chose `preset` writes on activation (and that its compare and done steps list)."""
    return (*ACTIVATION_KEYS, *ONE_NODE_KEYS) if preset == SAME_OWNER else ACTIVATION_KEYS


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
    them differently (the rows outside `activation_keys(preset)`). Shown on the done step with a link to Policy."""
    rest = tuple(k for k in KEYS if k not in activation_keys(preset))
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
    `keys=activation_keys(preset)` for what an enrolment's preset will change when the join completes."""
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
            return apply(session, settings, row.preset, bool(row.preset_admin), actor,
                         keys=activation_keys(row.preset),
                         target=activation_target(row))
    except Exception as exc:  # noqa: BLE001  (any failure of the local write, a database error included)
        groups.audit(session, actor, "group_policy_preset_failed", activation_target(row),
                     {"preset": row.preset, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return []
