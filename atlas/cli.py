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
    return s, make_sessionmaker(make_engine(s.database_url))


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("atlas.main:app_factory", factory=True, host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips)
    return 0


def cmd_migrate(args) -> int:
    upgrade(get_settings().database_url, args.revision)
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
        ident = load_identity(session)
        session.commit()
    pub = ident.trust_public_b64
    print(f"node_id:    {ident.node_id}")
    print(f"public_key: {pub}")
    print(f"signing_key_id: {ident.key_id}")
    print(f"mirrors trust this node with:\n  ATLAS_TRUSTED_KEYS='{json.dumps({ident.node_id: pub})}'")
    assert key_id_for(__import__("base64").b64decode(pub)) == ident.node_id
    return 0


def cmd_secret_key(_) -> int:
    import secrets

    print(secrets.token_urlsafe(32))
    return 0


def cmd_sync(_) -> int:
    settings, maker = _maker()
    if not settings.upstream_url:
        print("ATLAS_UPSTREAM_URL is not set", file=sys.stderr)
        return 2
    with maker() as session:
        ctx = Ctx(settings, load_identity(session))
        session.commit()
    with httpx.Client() as client:
        n = sync.pull_all(maker, ctx, client, settings.upstream_url, settings.upstream_token)
    print(f"applied {n} entries")
    return 0


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


def cmd_import_legacy(args) -> int:
    from .legacy_import import import_legacy

    settings, maker = _maker()
    with maker() as session:
        ctx = Ctx(settings, load_identity(session))
        stats = import_legacy(session, ctx, args.path)
        session.commit()
    print(json.dumps(stats))
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
    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    a = sub.add_parser("make-admin")
    a.add_argument("username")
    a.set_defaults(fn=cmd_make_admin)
    sub.add_parser("cache-media").set_defaults(fn=cmd_cache_media)
    i = sub.add_parser("import-legacy")
    i.add_argument("path", help="path to the demo-1 SQLite database")
    i.set_defaults(fn=cmd_import_legacy)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
