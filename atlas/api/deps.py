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


def require(scope: str):
    def dep(
        request: Request,
        st: Annotated[AppState, Depends(state)],
        session: Annotated[Session, Depends(get_session, scope="function")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> auth.Principal:
        principal = auth.authenticate(session, authorization)
        key = _client_key(request, principal)
        ok, retry = ratelimit.hit(st.maker, key, st.ctx.settings.rate_limit_per_minute if principal else 60)
        if not ok:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry)})
        if principal is None:
            raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
        if not principal.allows(scope):
            raise HTTPException(403, f"token lacks the {scope!r} scope")
        return principal

    dep.required_scope = scope  # type: ignore[attr-defined]  # read by tools/gen_docs.py
    return dep
