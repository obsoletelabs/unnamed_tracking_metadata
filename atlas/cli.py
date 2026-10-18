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


def cmd_migrate(_) -> int:
    upgrade(get_settings().database_url)
    print("database is up to date")
    return 0


def cmd_token(args) -> int:
    _, maker = _maker()
    with maker() as session:
        row, secret = auth.create_token(session, args.name, args.scopes.split(","), args.days)
        session.commit()
    print(json.dumps({"id": row.id, "scopes": row.scopes, "token": secret}))
    return 0


def cmd_node(_) -> int:
    settings, maker = _maker()
    with maker() as session:
        ident = load_identity(session)
        session.commit()
    pub = ident.public_b64
    print(f"node_id:    {ident.node_id}")
    print(f"public_key: {pub}")
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
    while (n := media.cache_pending(maker, settings)):
        total += n
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
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    t = sub.add_parser("token")
    t.add_argument("--name", required=True)
    t.add_argument("--scopes", required=True, help="comma separated: read,contribute,curate,sync,admin")
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
