from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), index=True)
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    principal_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    client_address: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")


def principal_hash(authorization: str) -> str:
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest() if authorization else ""


def record_api_mutation(engine, *, request_id: str, method: str, path: str, status_code: int,
                        authorization: str, client_address: str, detail: str = "") -> None:
    from sqlalchemy import insert

    with engine.begin() as connection:
        connection.execute(insert(SecurityAuditEvent.__table__).values(
            request_id=request_id[:100], method=method[:10], path=path[:500], status_code=status_code,
            principal_hash=principal_hash(authorization), client_address=client_address[:100], detail=detail[:1000],
        ))
