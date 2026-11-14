from __future__ import annotations

import argparse
import json
import sys

import httpx

from . import auth, sync
from .db import make_engine, make_sessionmaker, upgrade
from .service import Ctx
from .settings import get_settings
from .signing import key_id_for, load_identity


def _maker():
    s = get_settings()
    return s, make_sessionmaker(make_engine(s.database_url), s)


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("atlas.main:app_factory", factory=True, host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips)
    return 0


def cmd_migrate(args) -> int:
    settings = get_settings()
    upgrade(settings.database_url, args.revision, settings=settings)
    print("database is up to date" if args.revision == "head" else f"database is at revision {args.revision}")
    return 0


def cmd_backup(args) -> int:
    from pathlib import Path

    from . import backup

    settings = get_settings()
    if args.action != "create" and not args.file:
        print(f"backup {args.action}: give the archive path", file=sys.stderr)
        return 2
    try:
        if args.action == "create":
            out = Path(args.file or f"atlas-{__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz")
            m = backup.create(settings, make_engine(settings.database_url), out, include_media=not args.no_media)
            rows = sum(m["tables"].values())
            print(f"wrote {out} ({rows} rows, {sum(1 for f in m['files'] if f.startswith('media/'))} media files, "
                  f"revision {m['revision']}). It contains secrets: keep it private and encrypted off-site.")
        elif args.action == "verify":
            m = backup.verify(Path(args.file))
            print(f"ok: {sum(m['tables'].values())} rows, revision {m['revision']}, made {m['created_at']} by Atlas {m['atlas']}")  # noqa: E501
            if args.secret_check:
                c = backup.secret_check(settings, Path(args.file))
                if c["signing_key"] == "plaintext":
                    print("warning: the signing key in this backup is NOT encrypted (made without ATLAS_SECRET_KEY)",
                          file=sys.stderr)
                print(f"secrets ok: signing key {c['signing_key'] or 'absent'}"
                      f"{' (key id ' + c['signing_key_id'] + ')' if c.get('signing_key_id') else ''}, "
                      f"{c['secrets']} stored secrets decrypt with ATLAS_SECRET_KEY")
        else:
            loaded = backup.restore(settings, make_engine(settings.database_url), Path(args.file), force=args.force,
                                    include_media=not args.no_media)
            print(f"restored {sum(loaded.values())} rows; now run `atlas migrate` if this backup is older than the software")
    except backup.BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_token(args) -> int:
    _, maker = _maker()
    with maker() as session:
        row, secret = auth.create_token(session, args.name, args.scopes.split(","), args.days)
        session.commit()
    print(json.dumps({"id": row.id, "scopes": row.scopes, "token": secret}))
    return 0


def cmd_recover_mirror(args) -> int:
    from pathlib import Path

    from . import backup, promotion

    settings = get_settings()
    if not args.old_authority_fenced:
        print("recover-mirror: fence the old authority before cutover and confirm --old-authority-fenced",
              file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url)
    try:
        result = promotion.candidate(settings, engine, args.from_authority, args.confirm_mirror, Path(args.output))
    except (promotion.PromotionError, backup.BackupError) as exc:
        print(f"recover-mirror: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2))
    print("Offline candidate only. Keep the archive private: it contains a new signing key. "
          "Restore to an empty node, create fresh credentials and explicitly re-pin followers.", file=sys.stderr)
    return 0


def cmd_node(_) -> int:
    settings, maker = _maker()
    with maker() as session:
        ident = load_identity(session, settings)
        session.commit()
    pub = ident.trust_public_b64
    print(f"node_id:    {ident.node_id}")
    print(f"public_key: {pub}")
    print(f"signing_key_id: {ident.key_id}")
    print(f"mirrors trust this node with:\n  ATLAS_TRUSTED_KEYS='{json.dumps({ident.node_id: pub})}'")
    assert key_id_for(__import__("base64").b64decode(pub)) == ident.node_id
    return 0


def cmd_secrets(args) -> int:
    from . import secrets_box

    settings, maker = _maker()
    with maker() as session:
        try:
            counts = secrets_box.rewrap(session, settings)
        except secrets_box.SecretsError as exc:
            print(f"secrets rewrap: {exc}", file=sys.stderr)
            return 1
        session.commit()
    print("re-encrypted with the current ATLAS_SECRET_KEY: " + ", ".join(f"{v} {k.replace('_', ' ')}"
                                                                        for k, v in counts.items()))
    print("ATLAS_SECRET_KEY_PREVIOUS can be removed once every replica runs with the new secret and older backups "
          "are no longer needed (they still need the old secret).")
    return 0


def cmd_secret_key(_) -> int:
    import secrets

    print(secrets.token_urlsafe(32))
    return 0


# `atlas sync status|replay` exit codes (CONTRACTS-v1 C1.9)
EXIT_CONVERGED, EXIT_PARKED, EXIT_REFUSED = 0, 3, 4
# The authority's signed history no longer matches what this node verified (CONTRACT-AMENDMENTS A3 #11).
EXIT_REWOUND = 5


def _ctx(settings, maker):
    from . import runtime

    with maker() as session:
        ctx = Ctx(runtime.effective(settings, runtime.load(session)), load_identity(session, settings))
        session.commit()
    return ctx


def _exit_for(upstreams: dict) -> int:
    if any(u.get("refused") for u in upstreams.values()):
        return EXIT_REFUSED
    if any(u.get("rewound") for u in upstreams.values()):
        return EXIT_REWOUND
    if any(u.get("parked") for u in upstreams.values()):
        return EXIT_PARKED
    return EXIT_CONVERGED


def _describe(node: str, u: dict) -> str:
    lines = [f"{node}: {u['state']}  (this node speaks {u['own_protocol']}, upstream {u['upstream_protocol'] or 'unknown'})",
             f"  heads: observed {u['observed_head']}  downloaded {u['downloaded_head']}  verified {u['verified_head']}"
             f"  applied {u['applied_head']}"]
    if u.get("parked"):
        p = u["parked"]
        lines.append(f"  parked: {p['count']} entries from seq {p['first_seq']} ({p['first_reason']}, kind "
                     f"{p['first_kind']!r}){'; backlog full, downloading paused' if p['backlog_full'] else ''}")
        lines.append(f"    {p['first_error']}")
        lines.append("    upgrade Atlas and run `atlas sync replay`, or `atlas sync skip` it if you accept the loss")
    if u["quarantined"]["count"]:
        q = u["quarantined"]
        lines.append(f"  quarantined: {q['count']} entries (oldest seq {q['oldest_seq']}); retried automatically")
    if u.get("refused"):
        lines.append(f"  refused: {u['refused'].get('reason')}")
    if u.get("rewound"):
        lines.append(f"  rewound: {u['rewound'].get('reason')}")
        lines.append("    nothing from it is applied until it serves the entries this node verified again (#15 / #24)")
    return "\n".join(lines)


def cmd_sync(args) -> int:
    settings, maker = _maker()
    action = getattr(args, "action", None)
    if action is None:
        if not settings.upstream_url:
            print("ATLAS_UPSTREAM_URL is not set", file=sys.stderr)
            return 2
        ctx = _ctx(settings, maker)
        with httpx.Client() as client:
            n = sync.pull_all(maker, ctx, client, settings.upstream_url, settings.upstream_token)
        print(f"applied {n} entries")
        return 0
    ctx = _ctx(settings, maker)
    if action == "status":
        with maker() as session:
            upstreams = sync.status(session, ctx)["upstreams"]
        if args.node:
            upstreams = {k: v for k, v in upstreams.items() if k == args.node}
            if not upstreams:
                print(f"no upstream feed {args.node}", file=sys.stderr)
                return 2
        if args.json:
            print(json.dumps({"upstreams": upstreams}, indent=2, sort_keys=True))
        else:
            print("\n".join(_describe(n, u) for n, u in upstreams.items()) or "no upstream feeds")
        return _exit_for(upstreams)
    if action == "replay":
        with maker() as session:
            if args.rescan:
                nodes = [args.node] if args.node else sync.upstream_nodes(session)
                counts = {n: sync.rescan(session, n) for n in nodes}
                session.commit()
                for n, c in counts.items():
                    print(f"{n}: scanned {c['scanned']} applied entries; recorded {c['parked']} parked and "
                          f"{c['quarantined']} quarantined that earlier releases skipped. Nothing was re-applied; "
                          "run `atlas sync replay --historical` to re-apply them in feed order.")
                    if c.get("waiting"):
                        print(f"  {c['waiting']} entries now wait for a record the feed has not introduced yet; they "
                              "apply when it arrives.")
                upstreams = sync.status(session, ctx)["upstreams"]
                return _exit_for({n: upstreams[n] for n in nodes if n in upstreams})
            result = sync.replay(session, ctx, args.node, limit=args.limit, historical=args.historical)
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
        for n, r in result.items():
            verb = "would apply" if args.dry_run else "applied"
            print(f"{n}: {verb} {r['applied']} entries ({r['applied_before']} -> {r['applied_head']} of "
                  f"{r['verified_head']} verified); state {r['state']}")
            if args.historical:
                print(f"  re-applied {r['reapplied']} retained entries in feed order; {r['recovered_parked']} "
                      "entries earlier releases skipped are now applied")
            if r["historical_parked"] is not None:
                print(f"  entries earlier releases skipped are parked from seq {r['historical_parked']}; "
                      "`atlas sync replay --historical` re-applies them in feed order")
            if r["parked"]:
                p = r["parked"]
                print(f"  still parked at seq {p['first_seq']}: {p['first_error']}")
            if r["refused"]:
                print(f"  upstream refused: {r['refused'].get('reason')}")
            if r["rewound"]:
                print(f"  upstream rewound: {r['rewound'].get('reason')}")
        return _exit_for(result)
    if action == "skip":
        with maker() as session:
            try:
                result = sync.skip(session, ctx, args.node, args.seq, args.reason)
            except ValueError as exc:
                print(f"sync skip: {exc}", file=sys.stderr)
                return 1
            session.commit()
        print(f"entry {args.seq} of {args.node} is now quarantined (skipped, audited); applied head is "
              f"{result['applied_head']}")
        return 0
    if action == "reconcile":
        if not args.dry_run:
            print("sync reconcile: only --dry-run (compare) is available in this release", file=sys.stderr)
            return 2
        from . import secrets_box
        from .models import Peer

        with maker() as session:
            from sqlalchemy import select

            peer = session.scalars(select(Peer).where(Peer.node_id == args.node, Peer.enabled.is_(True))).first()
            if peer is not None:
                url = peer.url
                token = secrets_box.decrypt(settings, peer.token_enc) if peer.token_enc else None
            elif settings.upstream_url and args.node in settings.trusted_keys:
                url, token = settings.upstream_url, settings.upstream_token
            else:
                print(f"sync reconcile: no enabled peer or ATLAS_UPSTREAM_URL serves {args.node}", file=sys.stderr)
                return 2
            with httpx.Client() as client:
                result = sync.reconcile(session, client, url, token, args.node)
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["match"] is None:
            return 2
        return 0 if result["match"] else 1
    raise AssertionError(action)  # pragma: no cover


def cmd_make_admin(args) -> int:
    from sqlalchemy import select

    from .models import User

    _, maker = _maker()
    with maker() as session:
        user = session.scalars(select(User).where(User.username == args.username.strip().lower())).first()
        if user is None:
            print("no such user", file=sys.stderr)
            return 1
        user.role = "admin"
        session.commit()
    print(f"{user.username} is now an administrator")
    return 0


def cmd_cache_media(_) -> int:
    from . import media

    settings, maker = _maker()
    total = 0
    try:
        while (n := media.cache_pending(maker, settings)):
            total += n
    except media.MediaStorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"cached {total} images")
    return 0


def cmd_media(args) -> int:
    """`atlas media backfill|stats`: measure images cached before migration 0035 (C12 §12.5.2) in bounded batches."""
    from . import media

    settings, maker = _maker()
    if args.action == "stats":
        with maker() as s:
            print({**media.stats(s), "unmeasured": media.pending_backfill(s)})
        return 0
    total = 0
    while (n := media.backfill(maker, settings, limit=args.batch)):
        total += n
        if args.limit and total >= args.limit:
            break
    with maker() as s:
        left = media.pending_backfill(s)
    print(f"measured {total} images; {left} still to measure")
    return 0


def cmd_import_legacy(args) -> int:
    from .legacy_import import import_legacy

    settings, maker = _maker()
    with maker() as session:
        ctx = Ctx(settings, load_identity(session, settings))
        stats = import_legacy(session, ctx, args.path)
        session.commit()
    print(json.dumps(stats))
    return 0


def cmd_dates(args) -> int:
    """`atlas dates rebuild`: re-derive every record's view with the C5.2 release-date merge (hierarchical vote,
    partial and regional dates), in batches under the writer lock. Resumable: the last finished record id is
    kept in node_state, so an interrupted run continues where it stopped; `--restart` starts over."""
    from . import dates_rebuild

    settings, maker = _maker()
    with maker() as session:
        ctx = Ctx(settings, load_identity(session, settings))
    done = dates_rebuild.run(maker, ctx, batch=args.batch, restart=args.restart, limit=args.limit)
    print(json.dumps(done))
    return 0


def cmd_match(args) -> int:
    """`atlas match explain-backfill`: rewrite every live record's explain to version 2 (trust class, reason,
    runners-up, conflicts; C11 §11.7) and fill the `field_conflict` index, in batches under the writer lock.
    Resumable like `atlas dates rebuild`. A record whose view the trust classes change is rebuilt (H34).
    `atlas match prune`: apply match decision retention now (the server also does it daily)."""
    from . import matching, provenance

    settings, maker = _maker()
    if args.action == "prune":
        with maker() as session:
            result = {"pruned": matching.prune_decisions(session, settings)}
            session.commit()
        print(json.dumps(result))
        return 0
    for warning in provenance.class_warning(settings):
        print(warning, file=sys.stderr)
    with maker() as session:
        ctx = Ctx(settings, load_identity(session, settings))
    print(json.dumps(provenance.backfill(maker, ctx, batch=args.batch, restart=args.restart, limit=args.limit)))
    return 0


def cmd_prices(args) -> int:
    """`atlas prices purge`: delete price observations past their retention now (the server also does it
    hourly). `atlas prices status`: counts, what the next purge removes, and the price channel's state."""
    from . import prices

    _settings, maker = _maker()
    with maker() as session:
        if args.action == "purge":
            result = {"purged": prices.purge(session)}
            session.commit()
        else:
            result = prices.status(session)
    print(json.dumps(result))
    return 0


def cmd_feed(args) -> int:
    """`atlas feed introduce --missing|--all [--dry-run]` (C13 §4.2): publish a `record` (or, below 1.5 with the
    bridge on, a bridge observation) for records this feed names but never introduced (`--missing`), or for every
    live record (`--all`). In batches of their own transactions, resumable. Exit 2 when refused."""
    from . import feed

    settings, maker = _maker()
    scope = "all" if args.all else "missing"
    ctx = _ctx(settings, maker)  # the emit protocol raised in Admin → Peers is a runtime setting
    if args.dry_run:
        with maker() as session:
            print(json.dumps({"scope": scope, "would_introduce": feed.unintroduced_count(session, scope),
                              "mode": feed.introduction_mode(ctx.settings), **feed.records_summary(session)}))
        return 0
    total = 0
    while True:
        with maker() as session:
            try:
                result = feed.introduce_batch(session, ctx, scope, limit=args.batch)
            except feed.IntroduceRefused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
            session.commit()
        total += result["introduced"]
        if result["finished"] or (args.limit and total >= args.limit) or not result["examined"]:
            break
    print(json.dumps({**result, "introduced": total}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    s.set_defaults(fn=cmd_serve)
    mg = sub.add_parser("migrate")
    mg.add_argument("--revision", default="head", help="migrate to this revision instead of the latest (used before a restore)")  # noqa: E501
    mg.set_defaults(fn=cmd_migrate)
    b = sub.add_parser("backup", help="create, verify or restore a full backup archive")
    b.add_argument("action", choices=["create", "verify", "restore"])
    b.add_argument("file", nargs="?", help="archive path (create defaults to atlas-<timestamp>.tar.gz here)")
    b.add_argument("--no-media", action="store_true", help="leave cached artwork out (it can be downloaded again)")
    b.add_argument("--force", action="store_true", help="restore: replace any data already in the database")
    b.add_argument("--secret-check", action="store_true",
                   help="verify: also decrypt the signing key and stored secrets with ATLAS_SECRET_KEY")
    b.set_defaults(fn=cmd_backup)
    recovery = sub.add_parser("recover-mirror", help="build an offline authority candidate from one retained signed feed")
    recovery.add_argument("--from-authority", required=True, help="independently verified original authority node ID")
    recovery.add_argument("--confirm-mirror", required=True, help="exact source mirror node ID")
    recovery.add_argument("--old-authority-fenced", action="store_true", help="confirm the old writer is fenced")
    recovery.add_argument("--output", required=True, help="new private backup archive path; existing files are refused")
    recovery.set_defaults(fn=cmd_recover_mirror)
    t = sub.add_parser("token")
    t.add_argument("--name", required=True)
    t.add_argument("--scopes", required=True, help="comma separated: " + ",".join(auth.SCOPES))
    t.add_argument("--days", type=int)
    t.set_defaults(fn=cmd_token)
    sub.add_parser("node").set_defaults(fn=cmd_node)
    sub.add_parser("secret-key").set_defaults(fn=cmd_secret_key)
    sc = sub.add_parser("secrets", help="manage secrets stored encrypted in the database")
    sc.add_argument("action", choices=["rewrap"],
                    help="rewrap: re-encrypt the signing key and stored secrets with the current ATLAS_SECRET_KEY "
                         "(after moving the old one to ATLAS_SECRET_KEY_PREVIOUS)")
    sc.set_defaults(fn=cmd_secrets)
    sy = sub.add_parser("sync", help="pull from ATLAS_UPSTREAM_URL now; or status, replay, skip, reconcile")
    sy.set_defaults(fn=cmd_sync, action=None)
    sya = sy.add_subparsers(dest="action")
    st = sya.add_parser("status", help="heads, parked and quarantined entries per upstream "
                                         "(exit 3 parked, 4 refused, 5 rewound)")
    st.add_argument("--node", help="one upstream node id")
    st.add_argument("--json", action="store_true")
    rp = sya.add_parser("replay", help="re-classify parked entries and apply in order until the next park")
    rp.add_argument("--node", help="one upstream node id (default: all)")
    rp.add_argument("--dry-run", action="store_true", help="report what would apply, change nothing")
    rp.add_argument("--limit", type=int, help="apply at most this many entries per upstream")
    rp.add_argument("--rescan", action="store_true",
                    help="one-time recovery: record entries earlier releases silently skipped (classification only)")
    rp.add_argument("--historical", action="store_true",
                    help="re-apply the retained entries from the oldest one an earlier release skipped, in feed order")
    sk = sya.add_parser("skip", help="admin escape hatch: mark one parked entry quarantined (audited)")
    sk.add_argument("--node", required=True)
    sk.add_argument("--seq", type=int, required=True)
    sk.add_argument("--reason", required=True, help="written to the audit log")
    rc = sya.add_parser("reconcile", help="compare this node's view with the upstream's /sync/digest")
    rc.add_argument("--node", required=True)
    rc.add_argument("--dry-run", action="store_true", help="compare only (the only mode in this release)")
    a = sub.add_parser("make-admin")
    a.add_argument("username")
    a.set_defaults(fn=cmd_make_admin)
    sub.add_parser("cache-media").set_defaults(fn=cmd_cache_media)
    md = sub.add_parser("media", help="cached artwork: measure images stored before 0035 (C12), or show counts")
    md.add_argument("action", choices=["backfill", "stats"])
    md.add_argument("--batch", type=int, default=200, help="images per transaction (default 200)")
    md.add_argument("--limit", type=int, help="stop after about this many images (the next run continues)")
    md.set_defaults(fn=cmd_media)
    dt = sub.add_parser("dates", help="release dates: re-derive views with partial and regional dates (C5.2)")
    dt.add_argument("action", choices=["rebuild"])
    dt.add_argument("--batch", type=int, default=500, help="records per transaction (default 500)")
    dt.add_argument("--limit", type=int, help="stop after this many records (the next run continues)")
    dt.add_argument("--restart", action="store_true", help="start from the first record again")
    dt.set_defaults(fn=cmd_dates)
    mt = sub.add_parser("match", help="matching and provenance (C11): explain v2 backfill, decision retention")
    mt.add_argument("action", choices=["explain-backfill", "prune"])
    mt.add_argument("--batch", type=int, default=500, help="records per transaction (default 500)")
    mt.add_argument("--limit", type=int, help="stop after this many records (the next run continues)")
    mt.add_argument("--restart", action="store_true", help="start from the first record again")
    mt.set_defaults(fn=cmd_match)
    pr = sub.add_parser("prices", help="price observations (C8): purge expired rows now, or show status")
    pr.add_argument("action", choices=["purge", "status"])
    pr.set_defaults(fn=cmd_prices)
    fd = sub.add_parser("feed", help="this node's signed feed (C13): introduce records it names but never published")
    fda = fd.add_subparsers(dest="action", required=True)
    intro = fda.add_parser("introduce", help="sign a `record` for records followers cannot know yet")
    which = intro.add_mutually_exclusive_group(required=True)
    which.add_argument("--missing", action="store_true", help="records entries name but nothing introduced")
    which.add_argument("--all", action="store_true", help="every live record not introduced yet")
    intro.add_argument("--dry-run", action="store_true", help="count only, sign nothing")
    intro.add_argument("--batch", type=int, default=500, help="records per transaction (default 500)")
    intro.add_argument("--limit", type=int, help="stop after about this many records (the next run continues)")
    intro.set_defaults(fn=cmd_feed)
    i = sub.add_parser("import-legacy")
    i.add_argument("path", help="path to the demo-1 SQLite database")
    i.set_defaults(fn=cmd_import_legacy)
    from . import group_cli

    group_cli.register(sub)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
