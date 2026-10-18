from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from .. import auth, ratelimit
from ..providers import Fetcher
from ..service import Ctx


@dataclass
class AppState:
    ctx: Ctx
    maker: sessionmaker[Session]
    providers: Fetcher


def state(request: Request) -> AppState:
    return request.app.state.atlas


def get_session(st: Annotated[AppState, Depends(state)]) -> Iterator[Session]:
    session = st.maker()
    try:
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
        session: Annotated[Session, Depends(get_session)],
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

    return dep
