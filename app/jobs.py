from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .main import Base, SessionLocal


class AsyncJob(Base):
    __tablename__ = "async_jobs"
    __table_args__ = (UniqueConstraint("job_type", "idempotency_key", name="uq_async_job_type_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str] = mapped_column(String(120), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def enqueue_job(db, job_type: str, payload: dict, idempotency_key: str, max_attempts: int = 5) -> AsyncJob:
    if not job_type or len(job_type) > 80:
        raise ValueError("job_type is required and must be at most 80 characters")
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValueError("idempotency_key is required and must be at most 200 characters")
    if max_attempts < 1 or max_attempts > 20:
        raise ValueError("max_attempts must be between 1 and 20")
    existing = db.scalar(select(AsyncJob).where(AsyncJob.job_type == job_type, AsyncJob.idempotency_key == idempotency_key))
    if existing:
        return existing
    job = AsyncJob(job_type=job_type, idempotency_key=idempotency_key, payload_json=json.dumps(payload, separators=(",", ":")), max_attempts=max_attempts)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def reclaim_stale_jobs(db, stale_after_seconds: int = 900) -> int:
    if stale_after_seconds < 60:
        raise ValueError("stale_after_seconds must be at least 60")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    jobs = db.scalars(select(AsyncJob).where(AsyncJob.status == "running", AsyncJob.locked_at < cutoff)).all()
    for job in jobs:
        job.status = "queued" if job.attempts < job.max_attempts else "dead"
        job.locked_at = None
        job.locked_by = ""
        job.available_at = datetime.now(timezone.utc)
    db.commit()
    return len(jobs)


def claim_job(db, worker_id: str | None = None) -> AsyncJob | None:
    reclaim_stale_jobs(db)
    now = datetime.now(timezone.utc)
    job = db.scalar(
        select(AsyncJob)
        .where(AsyncJob.status == "queued", AsyncJob.available_at <= now, AsyncJob.attempts < AsyncJob.max_attempts)
        .order_by(AsyncJob.available_at, AsyncJob.id)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id or secrets.token_hex(8)
    db.commit()
    db.refresh(job)
    return job


def complete_job(db, job: AsyncJob) -> None:
    job.status = "completed"
    job.locked_at = None
    job.locked_by = ""
    job.completed_at = datetime.now(timezone.utc)
    job.last_error = ""
    db.commit()


def fail_job(db, job: AsyncJob, error: str, retry_delay_seconds: int = 30) -> None:
    if retry_delay_seconds < 1 or retry_delay_seconds > 86400:
        raise ValueError("retry_delay_seconds must be between 1 and 86400")
    job.last_error = error[:4000]
    job.locked_at = None
    job.locked_by = ""
    if job.attempts >= job.max_attempts:
        job.status = "dead"
    else:
        job.status = "queued"
        job.available_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds * (2 ** min(job.attempts - 1, 8)))
    db.commit()


def run_once(handler_registry: dict[str, callable], worker_id: str | None = None) -> bool:
    with SessionLocal() as db:
        job = claim_job(db, worker_id)
        if job is None:
            return False
        try:
            payload = json.loads(job.payload_json)
            handler = handler_registry.get(job.job_type)
            if handler is None:
                raise RuntimeError(f"No handler registered for job type {job.job_type}")
            handler(payload)
        except Exception as exc:
            fail_job(db, job, f"{type(exc).__name__}: {exc}")
        else:
            complete_job(db, job)
    return True
