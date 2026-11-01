"""`atlas group ...` (CONTRACT-C10 §8.2). Passphrases are read with getpass, never taken as arguments.

Exit codes: 0 ok, 1 refused, 2 usage, 3 degraded (`status`: expiring, fork, stale, resyncing, halted),
4 this node is not in a group.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import group_crypto as gc
from . import group_enrol, groups
from .models import GroupEnrolment, GroupRevoked
from .signing import load_identity

NOT_MEMBER, DEGRADED = 4, 3


def _maker():
    from .cli import _maker as cli_maker

    return cli_maker()


def _http() -> Any:
    import httpx

    return httpx.Client(timeout=15, trust_env=False)


def _ask(prompt: str, confirm: bool = False) -> str:
    value = getpass.getpass(prompt)
    if confirm and getpass.getpass("Again: ") != value:
        raise groups.GroupError("the two entries differ")
    return value


def _err(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _run(fn) -> int:
    settings, maker = _maker()
    with maker() as session:
        identity = load_identity(session, settings)
        try:
            code = fn(session, settings, identity)
            session.commit()
            return code
        except groups.GroupError as exc:
            session.rollback()
            return _err(str(exc), NOT_MEMBER if exc.code == "not_member" else 1)


def _group(session) -> groups.OwnerGroup:
    group = groups.load(session)
    if group is None:
        raise groups.GroupError("this node is not in a group", "not_member")
    return group


def cmd_create(args) -> int:
    count = {"1-of-1": 1, "2-of-3": 3}[args.recovery]
    out = Path(args.recovery_out)
    if not out.is_dir():
        return _err(f"--recovery-out {out} is not a directory", 2)

    def run(session, settings, identity):
        root = _ask("Root passphrase (14+ characters): ", confirm=True)
        phrases = [_ask(f"Passphrase for recovery file {i + 1} of {count}: ", confirm=True) for i in range(count)]
        created = groups.create_group(session, settings, identity, args.name, root, phrases,
                                      threshold=1 if count == 1 else 2, cli=True)
        paths = []
        for i, data in enumerate(created.recovery_files, start=1):
            path = out / f"atlas-group-{created.group.group_id}-recovery-{i}.json"
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            paths.append(path)
        session.flush()
        groups.confirm_recovery(session, settings, created.group, phrases[0])
        print(f"group_id: {groups.gc_fp(created.group.group_id)}")
        for path in paths:
            print(f"recovery file: {path}  (store it offline; it is the only way back if this node is lost)")
        return 0

    return _run(run)


def _status(session, settings, identity) -> tuple[dict, int]:
    group = _group(session)
    tr = groups.rebuild(session, group, identity.node_id, settings=settings)
    statuses = {m.node_id: m.status for m in session.scalars(select(groups.GroupMember).where(
        groups.GroupMember.group_id == group.group_id))}
    fresh = groups.freshness(session, group, identity.node_id, None, settings.group_view_max_age_hours)
    data = {"group_id": group.group_id, "name": group.name, "status": group.status, "root": group.root_state,
            "self_serial": group.self_serial, "members": len(tr.members()), "member_status": statuses,
            "crl_seq": groups.crl_seqs(session, group.group_id), "fresh": fresh.ok, "freshness": fresh.reason,
            "succession_pending": bool(groups.pending_successions(group))}
    degraded = group.status in ("resyncing", "halted") or any(s in ("expiring", "fork") for s in statuses.values()) \
        or (not fresh.ok and not fresh.waived) or data["succession_pending"]
    if group.status in ("left", "revoked"):
        return data, NOT_MEMBER
    return data, DEGRADED if degraded else 0


def cmd_status(args) -> int:
    def run(session, settings, identity):
        data, code = _status(session, settings, identity)
        if args.json:
            print(json.dumps(data, indent=1, sort_keys=True))
        else:
            print(f"group {data['name']} ({groups.gc_fp(data['group_id'])}): {data['status']}, root {data['root']}, "
                  f"{data['members']} members, fresh={data['fresh']} {data['freshness']}")
        return code

    return _run(run)


def cmd_members(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        groups.rebuild(session, group, identity.node_id, settings=settings)
        rows = [{"node_id": m.node_id, "fingerprint": groups.gc_fp(m.node_id), "name": m.name, "url": m.url,
                 "scopes": m.scopes, "serial": m.serial, "not_after": gc.stamp(groups.aware(m.not_after)),
                 "status": m.status, "self": m.node_id == identity.node_id}
                for m in session.scalars(select(groups.GroupMember).where(
                    groups.GroupMember.group_id == group.group_id).order_by(groups.GroupMember.name))]
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print(f"{r['fingerprint']}  {r['status']:<9} {r['name']:<24} {r['url']}  {','.join(r['scopes'])}")
        return 0

    return _run(run)


def cmd_invite(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        scopes = args.scopes.split(",") if args.scopes else list(groups.DEFAULT_SCOPES)
        roles = [r for r in (args.grant_roles or "").split(",") if r]
        nodes = [n for n in (args.grant_nodes or "").split(",") if n]
        if "admin" in roles and not args.confirm_admin:
            raise groups.GroupError("letting the new node grant 'admin' needs --confirm-admin (§1.3)")
        delegate = None
        if args.delegate_enrol:
            scopes = [*scopes, "group.enrol"]
        if roles:
            scopes = [*scopes, "grant.issue"]
        if args.delegate_enrol or roles:
            delegate = groups.default_delegate(args.delegate_enrol or 0, roles, nodes)
        row, code = group_enrol.create_invite(session, settings, group, identity, scopes, delegate, cli=True)
        print(f"address: {settings.public_url}")
        print(f"code:    {code}")
        print(f"expires: {gc.stamp(groups.aware(row.expires_at))} (10 minutes; single use)")
        return 0

    return _run(run)


def cmd_join(args) -> int:
    def run(session, settings, identity):
        http = _http()
        row, sas, inviter = group_enrol.start_join(session, settings, identity, args.url, args.code, http,
                                                   name=args.name, cli=True)
        print(f"enrolment: {row.id}")
        print(f"inviter:   {groups.gc_fp(inviter['node_id'])}  {inviter['name']}  {inviter['url']}")
        print(f"SAS:       {sas}")
        print(f"Compare the SAS with the inviter's screen, then run: atlas group confirm {row.id} --sas {sas}")
        return 0

    return _run(run)


def cmd_confirm(args) -> int:
    def run(session, settings, identity):
        row = session.get(GroupEnrolment, args.enrolment_id)
        if row is None:
            raise groups.GroupError("no such enrolment")
        if group_enrol.sas_of(settings, row) != args.sas.strip():
            raise groups.GroupError("that SAS does not match this enrolment: do not confirm; cancel the invite")
        if row.side == "inviter":
            group = _group(session)
            kind = json.loads(groups.open_secret(settings, row.secret_enc)).get("kind")
            passphrase = _ask("Root passphrase: ") if kind == "root" else None
            cert = group_enrol.confirm_inviter(session, settings, group, identity, row.id, passphrase, http=_http(),
                                               cli=True)
            print(f"issued {cert['body']['serial']} for {groups.gc_fp(cert['body']['member']['node_id'])}")
            return 0
        outcome = group_enrol.confirm_joiner(session, settings, identity, row.id, _http(), cli=True)
        print(f"enrolment {outcome}" + (" (run the command again once the inviter confirmed)"
                                         if outcome == "pending" else ""))
        return 0 if outcome != "aborted" else 1

    return _run(run)


def cmd_renew(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        root = groups.unlock_root(session, settings, group, _ask("Root passphrase: "))
        issued = groups.renew(session, settings, group, identity, None if args.all else [args.node], root=root,
                              cli=True)
        for cert in issued:
            print(f"renewed {cert['body']['member']['node_id']} as {cert['body']['serial']}")
        return 0

    return _run(run)


def cmd_revoke(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        suspect = gc.parse_time(args.suspect_since) if args.suspect_since else None
        root = groups.unlock_root(session, settings, group, _ask("Root passphrase: ")) \
            if group.root_state == "holder" else None
        crl = groups.revoke_node(session, settings, group, identity, args.node, args.reason, root=root,
                                 suspect_since=suspect, cli=True)
        print(f"revoked {args.node} ({args.reason}) in {crl['id']}")
        return 0

    return _run(run)


def _root_or_none(session, settings, group, use_root: bool):
    return groups.unlock_root(session, settings, group, _ask("Root passphrase: ")) if use_root else None


def cmd_grant(args) -> int:
    """`atlas group grant --account UID --role ROLE --nodes N1,N2|* --days 30` (§8.2). Signs as root when this
    node holds the root (passphrase asked), otherwise with this node's `grant.issue` delegation."""
    def run(session, settings, identity):
        group = _group(session)
        nodes = ["*"] if args.nodes.strip() == "*" else [n.strip() for n in args.nodes.split(",") if n.strip()]
        use_root = group.root_state == "holder" and not args.as_node
        root = _root_or_none(session, settings, group, use_root)
        obj = groups.issue_grant(session, settings, group, identity, account_uid=args.account, role=args.role,
                                 nodes=nodes, days=args.days, root=root, account_label=args.label or "",
                                 note=args.note or "", cli=True)
        print(f"granted {args.role} to {args.account} on {','.join(nodes)} until {obj['body']['not_after']}: "
              f"{obj['body']['serial']}")
        return 0

    return _run(run)


def cmd_grants(args) -> int:
    def run(session, settings, identity):
        rows = groups.grant_rows(session, _group(session), identity.node_id)
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                print(f"{r['serial']}  {r['status']:<10} {r['role']:<11} {r['account_uid']}  "
                      f"{','.join(r['nodes'])}  until {r['not_after']}")
            if not rows:
                print("no grants")
        return 0

    return _run(run)


def cmd_revoke_grant(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        issued_here = (groups.parse_serial(args.serial) or groups.Serial("", 0, None)).issuer == identity.node_id
        root = _root_or_none(session, settings, group, not issued_here and group.root_state == "holder")
        crl = groups.revoke_grant(session, settings, group, identity, args.serial, root=root, cli=True)
        print(f"revoked grant {args.serial} in {crl['id']}")
        return 0

    return _run(run)


def cmd_leave(_args) -> int:
    def run(session, settings, identity):
        crl = groups.leave(session, settings, _group(session), identity, cli=True)
        print("left the group" + (f"; other members learn it from {crl['id']}" if crl else ""))
        return 0

    return _run(run)


def cmd_crl(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        rows = [{"serial": r.serial, "issuer": r.issuer, "reason": r.reason, "node_id": r.node_id,
                 "revoked_at": gc.stamp(groups.aware(r.revoked_at))}
                for r in session.scalars(select(GroupRevoked).where(GroupRevoked.group_id == group.group_id))]
        print(json.dumps({"crl_seq": groups.crl_seqs(session, group.group_id), "revoked": rows}, indent=1)
              if args.json else "\n".join(f"{r['serial']}  {r['reason']}" for r in rows) or "no revocations")
        return 0

    return _run(run)


def cmd_sync(_args) -> int:
    from .api import group_routes

    def run(session, settings, identity):
        outcomes = group_routes.pull(session, settings, identity, _group(session), _http())
        for node, outcome in outcomes.items():
            print(f"{groups.gc_fp(node)}  {outcome}")
        return 0 if all(v == "ok" for v in outcomes.values()) else DEGRADED

    return _run(run)


def cmd_resync(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        if group.status != "resyncing":
            print(f"the group is {group.status}; nothing to do")
            return 0
        if not args.skip:
            raise groups.GroupError("pull every member with `atlas group sync`; or --skip (audited)")
        group.status = "active"
        groups.audit(session, None, "group_resync_skipped", group.group_id, {}, cli=True)
        print("resync skipped (audited)")
        return 0

    return _run(run)


def cmd_new_epoch(_args) -> int:
    """§3.6: open a new epoch of this node's change streams (`reason: manual`). After a clone fork, run it on the
    real node once the copy is shut down: receivers accept the new epoch and lift their halt (the disputed history
    stays closed at what each receiver holds)."""
    from . import group_channel

    def run(session, settings, identity):
        group = _group(session)
        if not group_channel.may_emit(session, group, identity):
            raise groups.GroupError("this node cannot emit now (not active, or no in-force certificate with "
                                    "profile.sync)")
        epoch = group_channel.open_epoch(session, group, identity, "manual")
        print(f"new epoch {epoch}")
        return 0

    return _run(run)


def _open_files(paths: list[str]) -> list[gc.RecoveryKeys]:
    opened = []
    for i, path in enumerate(paths, start=1):
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise groups.GroupError(f"cannot read recovery file {path}: {exc}") from None
        opened.append(gc.open_recovery_file(data, _ask(f"Passphrase of recovery file {i} ({Path(path).name}): ")))
    return opened


def cmd_root(args) -> int:
    def run(session, settings, identity):
        group = _group(session)
        if args.root_cmd == "export-recovery":
            root = groups.unlock_root(session, settings, group, _ask("Root passphrase: "))
            phrase = _ask("Passphrase for the new recovery file (14+ characters): ", confirm=True)
            try:
                gc.check_passphrase(phrase, groups._names(settings, None, group.name))
            except gc.WeakPassphrase as exc:
                raise groups.GroupError(f"passphrase refused: {exc}") from None
            recovery = None
            if args.recovery_file:
                old = gc.open_recovery_file(json.loads(Path(args.recovery_file).read_text("utf-8")),
                                            _ask("Passphrase of that recovery file: "))
                recovery = old.recovery
            data = gc.make_recovery_file(group.genesis, list(group.succession or []), recovery, root, phrase,
                                         gc.stamp())
            Path(args.out).write_text(json.dumps(data, indent=1), encoding="utf-8")
            groups.audit(session, None, "group_root_exported", group.group_id, {"out": "file"}, cli=True)
            print(f"wrote {args.out}; verify it with `atlas group root verify-recovery --recovery-file {args.out}`")
            return 0
        if args.root_cmd == "verify-recovery":
            data = json.loads(Path(args.recovery_file).read_text("utf-8"))
            keys = gc.open_recovery_file(data, _ask("Recovery-file passphrase: "))
            if keys.group_id != group.group_id:
                raise groups.GroupError("that file belongs to another group")
            holds_root = keys.root is not None and gc.key_id_of(gc.ed_public_b64(keys.root)) == group.root_key_id
            if holds_root:
                session.merge(groups.NodeState(key=groups.RECOVERY_VERIFIED, value=gc.stamp()))
            print("recovery file verified" + (" (holds the current root key)" if holds_root else " (no root key)"))
            return 0
        if args.root_cmd == "import":
            keys = _open_files([args.recovery_file])[0]
            phrase = _ask("New root passphrase for this node (14+ characters): ", confirm=True)
            moved = groups.import_root(session, settings, group, identity, keys, phrase, args.old_root, cli=True)
            print(f"the group root is now on this node; other members learn it from {moved['id']}")
            print("this node is resyncing: run `atlas group sync` (or `atlas group resync --skip` if the old root "
                  "node is gone for good) before any root operation")
            return 0
        if args.root_cmd == "succession":
            out = Path(args.recovery_out) if args.recovery_out else None
            files = _open_files(args.recovery_file)
            one_of_one = group.recovery["threshold"] == 1 and len(group.recovery["keys"]) == 1
            if one_of_one and (out is None or not out.is_dir()):
                raise groups.GroupError("--recovery-out DIR is needed: a 1-of-1 succession writes a new recovery file")
            exclude = tuple(n.strip() for n in args.exclude_node or [])
            root_phrase = _ask("Passphrase for the new root key (14+ characters): ", confirm=True)
            rec_phrase = _ask("Passphrase for the new recovery file (14+ characters): ", confirm=True) \
                if one_of_one else None
            obj, new_files = groups.issue_succession(session, settings, group, identity, files, root_phrase,
                                                     exclude_nodes=exclude, reason=args.reason,
                                                     delay_hours=args.delay_hours, new_recovery_passphrase=rec_phrase,
                                                     cli=True)
            body = obj["body"]
            print(f"succession {body['index']}: new root {groups.gc_fp(body['new_root_key_id'])}, "
                  f"{len(body['carry'])} members carried, activates in {body['activation_delay_hours']} h")
            for data in new_files:
                path = out / f"atlas-group-{group.group_id}-recovery-succession-{body['index']}.json"  # type: ignore[operator]
                path.write_text(json.dumps(data, indent=1), encoding="utf-8")
                print(f"new recovery file: {path}  (store it offline; the old file holds the replaced root key)")
            return 0
        if args.root_cmd == "succession-cancel":
            obj = groups.cancel_succession(session, settings, group, _open_files(args.recovery_file),
                                           index=args.index, cli=True)
            print(f"succession {obj['body']['index']} cancelled; other members learn it on their next pull")
            return 0
        return _err("unknown root command", 2)

    try:
        return _run(run)
    except gc.WrongPassphrase:
        return _err("wrong passphrase")
    except gc.GroupCryptoError as exc:
        return _err(str(exc))


def register(sub) -> None:
    g = sub.add_parser("group", help="owner federation group: create, invite, join, members, revoke, leave")
    ga = g.add_subparsers(dest="group_cmd", required=True)
    c = ga.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--recovery", choices=("1-of-1", "2-of-3"), default="1-of-1")
    c.add_argument("--recovery-out", required=True, help="directory for the recovery file(s)")
    c.set_defaults(fn=cmd_create)
    s = ga.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    m = ga.add_parser("members")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_members)
    i = ga.add_parser("invite")
    i.add_argument("--scopes", help="comma separated; default profile.sync,group.relay,identity.assert")
    i.add_argument("--delegate-enrol", type=int, metavar="N", help="let the new node enrol up to N members")
    i.add_argument("--grant-roles", help="let the new node sign grants of these roles: moderator,curator,admin")
    i.add_argument("--grant-nodes", help="...on these member node ids; 'self' is the new node")
    i.add_argument("--confirm-admin", action="store_true", help="required to include 'admin' in --grant-roles")
    i.set_defaults(fn=cmd_invite)
    gr = ga.add_parser("grant", help="sign an administrative delegation grant (C10 §6.1)")
    gr.add_argument("--account", required=True, help="the group account id (account_uid)")
    gr.add_argument("--role", required=True, choices=groups.GRANT_ALL_ROLES)
    gr.add_argument("--nodes", required=True, help="N1,N2 (member node ids) or *")
    gr.add_argument("--days", type=int, default=groups.GRANT_DAYS)
    gr.add_argument("--label")
    gr.add_argument("--note")
    gr.add_argument("--as-node", action="store_true", help="sign with this node's grant.issue delegation, not root")
    gr.set_defaults(fn=cmd_grant)
    gl = ga.add_parser("grants")
    gl.add_argument("--json", action="store_true")
    gl.set_defaults(fn=cmd_grants)
    rg = ga.add_parser("revoke-grant")
    rg.add_argument("serial")
    rg.set_defaults(fn=cmd_revoke_grant)
    j = ga.add_parser("join")
    j.add_argument("--url", required=True)
    j.add_argument("--code", required=True)
    j.add_argument("--name")
    j.set_defaults(fn=cmd_join)
    cf = ga.add_parser("confirm")
    cf.add_argument("enrolment_id")
    cf.add_argument("--sas", required=True)
    cf.set_defaults(fn=cmd_confirm)
    r = ga.add_parser("renew")
    who = r.add_mutually_exclusive_group(required=True)
    who.add_argument("--node")
    who.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_renew)
    rv = ga.add_parser("revoke")
    rv.add_argument("--node", required=True)
    rv.add_argument("--reason", choices=("removed", "key_compromise"), default="removed")
    rv.add_argument("--suspect-since")
    rv.set_defaults(fn=cmd_revoke)
    ga.add_parser("leave").set_defaults(fn=cmd_leave)
    cr = ga.add_parser("crl")
    cr.add_argument("--json", action="store_true")
    cr.set_defaults(fn=cmd_crl)
    ga.add_parser("sync").set_defaults(fn=cmd_sync)
    rs = ga.add_parser("resync")
    rs.add_argument("--skip", action="store_true")
    rs.set_defaults(fn=cmd_resync)
    ne = ga.add_parser("new-epoch", help="open a new epoch of this node's change streams (after a clone fork)")
    ne.set_defaults(fn=cmd_new_epoch)
    ro = ga.add_parser("root")
    roa = ro.add_subparsers(dest="root_cmd", required=True)
    ex = roa.add_parser("export-recovery")
    ex.add_argument("--out", required=True)
    ex.add_argument("--recovery-file", help="an existing recovery file whose recovery key the new file keeps")
    ex.set_defaults(fn=cmd_root)
    vr = roa.add_parser("verify-recovery")
    vr.add_argument("--recovery-file", required=True)
    vr.set_defaults(fn=cmd_root)
    im = roa.add_parser("import", help="move the group root to this member from a recovery file holding it")
    im.add_argument("--recovery-file", required=True)
    im.add_argument("--old-root", required=True, choices=("decommissioned", "running"),
                    help="is the old root node destroyed, or still running?")
    im.set_defaults(fn=cmd_root)
    sc = roa.add_parser("succession", help="replace a stolen or lost root key with a recovery quorum")
    sc.add_argument("--recovery-file", action="append", required=True, help="repeat for a 2-of-3 quorum")
    sc.add_argument("--exclude-node", action="append", metavar="NODE_ID", help="do not carry this member")
    sc.add_argument("--reason", choices=("compromised", "lost"), default="compromised")
    sc.add_argument("--delay-hours", type=int, help="activation delay (at least the genesis policy, 24 by default)")
    sc.add_argument("--recovery-out", help="directory for the new recovery file (1-of-1)")
    sc.set_defaults(fn=cmd_root)
    cc = roa.add_parser("succession-cancel", help="cancel a pending root succession with a recovery quorum")
    cc.add_argument("--recovery-file", action="append", required=True)
    cc.add_argument("--index", type=int)
    cc.set_defaults(fn=cmd_root)

