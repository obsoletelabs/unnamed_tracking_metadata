from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from . import community, main
from .main import Base, Db, Game, require_api_token

router = APIRouter()
REPORT_REASONS = {"copyright", "spam", "abuse", "wrong-game", "misleading", "unsafe", "other"}
REPORT_STATUSES = {"open", "resolved", "dismissed"}


class CommunityReport(Base):
    __tablename__ = "community_reports"
    __table_args__ = (
        UniqueConstraint("reporter_id", "image_id", name="uq_community_reporter_image"),
        UniqueConstraint("reporter_id", "candidate_id", name="uq_community_reporter_candidate"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("game_images.id", ondelete="CASCADE"), nullable=True, index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("metadata_candidates.id", ondelete="CASCADE"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(30), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportCreate(BaseModel):
    image_id: int | None = Field(default=None, ge=1)
    candidate_id: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=30)
    details: str = Field(default="", max_length=2000)


class ReportResolution(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=2000)


def admin_user(request: Request, db: Db):
    user = main.current_user(request, db)
    if not user or not user.email_verified or user.email.lower() not in main.ADMIN_EMAILS:
        raise HTTPException(403, "Administrator access required")
    return user


def report_target(db, payload: ReportCreate):
    if (payload.image_id is None) == (payload.candidate_id is None):
        raise HTTPException(422, "Provide exactly one image_id or candidate_id")
    if payload.reason.strip().lower() not in REPORT_REASONS:
        raise HTTPException(422, "Unsupported report reason")
    if payload.image_id is not None:
        image = db.get(main.GameImage, payload.image_id)
        if not image:
            raise HTTPException(404, "Image not found")
        return image, None
    candidate = db.get(community.MetadataCandidate, payload.candidate_id)
    if not candidate:
        raise HTTPException(404, "Metadata candidate not found")
    return None, candidate


def serialize_report(db, report: CommunityReport) -> dict:
    game_id = None
    title = None
    if report.image_id is not None:
        image = db.get(main.GameImage, report.image_id)
        if image:
            game = db.get(Game, image.game_id)
            if game:
                game_id, title = game.canonical_id, game.title
    elif report.candidate_id is not None:
        candidate = db.get(community.MetadataCandidate, report.candidate_id)
        if candidate:
            game = db.get(Game, candidate.game_id)
            if game:
                game_id, title = game.canonical_id, game.title
    return {
        "id": report.id,
        "target_type": "image" if report.image_id is not None else "metadata",
        "image_id": report.image_id,
        "candidate_id": report.candidate_id,
        "game_id": game_id,
        "game_title": title,
        "reason": report.reason,
        "details": report.details,
        "status": report.status,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
        "review_note": report.review_note,
    }


@router.post("/api/v1/community/reports", status_code=201)
def create_report(payload: ReportCreate, db: Db, authorization: str | None = Header(default=None)):
    _, user = require_api_token(db, authorization, "write")
    if not user.email_verified:
        raise HTTPException(403, "Email verification is required for community reports")
    image, candidate = report_target(db, payload)
    existing_statement = select(CommunityReport).where(
        CommunityReport.reporter_id == user.id,
        CommunityReport.status == "open",
    )
    if image is not None:
        existing_statement = existing_statement.where(CommunityReport.image_id == image.id)
    else:
        existing_statement = existing_statement.where(CommunityReport.candidate_id == candidate.id)
    if db.scalar(existing_statement):
        raise HTTPException(409, "You already have an open report for this item")
    report = CommunityReport(
        reporter_id=user.id,
        image_id=image.id if image is not None else None,
        candidate_id=candidate.id if candidate is not None else None,
        reason=payload.reason.strip().lower(),
        details=payload.details.strip(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return serialize_report(db, report)


@router.get("/api/v1/community/reports")
def list_reports(request: Request, db: Db, status: str = "open"):
    admin_user(request, db)
    if status not in REPORT_STATUSES:
        raise HTTPException(422, "Unsupported report status")
    reports = db.scalars(
        select(CommunityReport)
        .where(CommunityReport.status == status)
        .order_by(CommunityReport.created_at.asc(), CommunityReport.id.asc())
        .limit(250)
    ).all()
    return {"status": status, "count": len(reports), "items": [serialize_report(db, report) for report in reports]}


@router.post("/api/v1/community/reports/{report_id}/resolve")
def resolve_report(report_id: int, payload: ReportResolution, request: Request, db: Db):
    user = admin_user(request, db)
    if payload.status not in {"resolved", "dismissed"}:
        raise HTTPException(422, "Report status must be resolved or dismissed")
    report = db.get(CommunityReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    report.status = payload.status
    report.reviewed_by = user.id
    report.review_note = payload.note.strip()
    report.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return serialize_report(db, report)
