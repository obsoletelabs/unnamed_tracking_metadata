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
        delegate = None
        if args.delegate_enrol:
            scopes = [*scopes, "group.enrol"]
            delegate = groups.default_delegate(args.delegate_enrol)
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
        return _err("unknown root command", 2)

    try:
        return _run(run)
    except gc.WrongPassphrase:
        return _err("wrong passphrase")


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
    i.set_defaults(fn=cmd_invite)
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
    ro = ga.add_parser("root")
    roa = ro.add_subparsers(dest="root_cmd", required=True)
    ex = roa.add_parser("export-recovery")
    ex.add_argument("--out", required=True)
    ex.add_argument("--recovery-file", help="an existing recovery file whose recovery key the new file keeps")
    ex.set_defaults(fn=cmd_root)
    vr = roa.add_parser("verify-recovery")
    vr.add_argument("--recovery-file", required=True)
    vr.set_defaults(fn=cmd_root)

