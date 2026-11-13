from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from .. import auth, ratelimit, runtime
from ..providers import Fetcher
from ..service import Ctx
from ..settings import Settings


@dataclass
class AppState:
    ctx: Ctx
    maker: sessionmaker[Session]
    providers: Fetcher
    base: Settings | None = None  # environment settings; admin overrides are layered on top
    http: Any = None  # outbound HTTP for admin-driven calls (peer probes, OIDC); replaceable in tests
    _stamp: str = "\0"
    _checked: float = 0.0

    def refresh(self, session: Session, force: bool = False) -> None:
        """Pick up admin setting changes (checked at most every few seconds per process)."""
        if self.base is None or (not force and time.monotonic() - self._checked < 5):
            return
        self._checked = time.monotonic()
        marker = runtime.stamp(session)
        if marker != self._stamp:
            self._stamp = marker
            fresh = runtime.effective(self.base, runtime.load(session))
            self.ctx.settings = fresh
            self.providers.settings = fresh
            self.providers.rebuild(fresh)


def state(request: Request) -> AppState:
    return request.app.state.atlas


def get_session(st: Annotated[AppState, Depends(state)]) -> Iterator[Session]:
    session = st.maker()
    try:
        st.refresh(session)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _client_key(request: Request, principal: auth.Principal | None) -> str:
    if principal:
        return f"t:{principal.token_id}"
    ip = request.client.host if request.client else "?"  # proxy headers are applied by uvicorn for trusted proxies only
    return f"ip:{ip}"[:128]


#: Who an anonymous client-API read is (CONTRACT-C15 D15.15 `public_read`): no token id, `read` only.
ANONYMOUS = auth.Principal("", "anonymous", frozenset({"read"}))


def require(scope: str, *, public_read: bool = False):
    """The token check for a route. `public_read=True` (the client API subset, A24): while the admin setting
    `public_read` is on, a request with **no** Authorization header reads as `ANONYMOUS`; a header that does not
    name a live token is still refused."""
    def dep(
        request: Request,
        st: Annotated[AppState, Depends(state)],
        session: Annotated[Session, Depends(get_session, scope="function")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> auth.Principal:
        from ..instruments import authorization as decisions

        principal = auth.authenticate(session, authorization, st.ctx.settings.self_service_sync_tokens)
        key = _client_key(request, principal)
        ok, retry = ratelimit.hit(st.maker, key, st.ctx.settings.rate_limit_per_minute if principal else 60)
        if not ok:
            decisions.inc(scope=scope, decision="denied", reason="rate_limited")
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry)})
        if principal is None and public_read and scope == "read" and not authorization and st.ctx.settings.public_read:
            decisions.inc(scope=scope, decision="allowed", reason="ok")
            return ANONYMOUS
        if principal is None:
            decisions.inc(scope=scope, decision="denied", reason="missing_token")
            raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
        if not principal.allows(scope):
            decisions.inc(scope=scope, decision="denied", reason="insufficient_scope")
            raise HTTPException(403, f"token lacks the {scope!r} scope")
        decisions.inc(scope=scope, decision="allowed", reason="ok")
        return principal

    dep.required_scope = scope  # type: ignore[attr-defined]  # read by tools/gen_docs.py
    dep.public_read = public_read  # type: ignore[attr-defined]
    return dep


def count_client(request: Request) -> None:
    """`atlas_client_requests_total{client}` (CONTRACT-C15 §10.2): every request to a client API route, labelled by
    the connector named in `User-Agent: atlas-connector-<host>/<version>` (closed set; anything else `other`)."""
    from .. import client_api
    from ..instruments import client_requests

    client_requests.inc(client=client_api.client_label(request.headers.get("user-agent")))
