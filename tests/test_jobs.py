from datetime import datetime, timezone

from app.jobs import claim_job, complete_job, enqueue_job, fail_job
from app.main import Base, SessionLocal, engine


def test_jobs_are_idempotent_and_retryable():
    from app.migrations import run_migrations

    Base.metadata.drop_all(engine)
    run_migrations()  # a fresh schema, as the application would create it
    with SessionLocal() as session:
        first = enqueue_job(session, "test", {"value": 1}, "same-key", max_attempts=2)
        second = enqueue_job(session, "test", {"value": 2}, "same-key", max_attempts=2)
        assert first.id == second.id

        claimed = claim_job(session, "worker-a")
        assert claimed is not None
        assert claimed.attempts == 1
        assert claimed.status == "running"

        fail_job(session, claimed, "temporary failure", retry_delay_seconds=1)
        assert claimed.status == "queued"
        claimed.available_at = datetime.now(timezone.utc)
        session.commit()

        claimed_again = claim_job(session, "worker-b")
        assert claimed_again is not None
        assert claimed_again.attempts == 2
        complete_job(session, claimed_again)
        assert claimed_again.status == "completed"
