from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import community
from .community_moderation import CommunityReport
from .main import User


@dataclass(frozen=True, slots=True)
class TrustScore:
    user_id: int
    score: float
    votes_cast: int
    positive_votes: int
    negative_votes: int
    reports_resolved_helpfully: int
    reports_dismissed: int


def calculate_trust(db: Session, user_id: int) -> TrustScore:
    user = db.get(User, user_id)
    if user is None:
        raise ValueError("unknown user")

    image_votes = db.execute(
        select(func.count(community.ImageVote.id), func.sum(community.ImageVote.value > 0), func.sum(community.ImageVote.value < 0))
        .where(community.ImageVote.user_id == user_id)
    ).one()
    metadata_votes = db.execute(
        select(func.count(community.MetadataVote.id), func.sum(community.MetadataVote.value > 0), func.sum(community.MetadataVote.value < 0))
        .where(community.MetadataVote.user_id == user_id)
    ).one()
    helpful_reports = db.scalar(
        select(func.count(CommunityReport.id)).where(
            CommunityReport.reporter_id == user_id,
            CommunityReport.status == "resolved",
        )
    ) or 0
    dismissed_reports = db.scalar(
        select(func.count(CommunityReport.id)).where(
            CommunityReport.reporter_id == user_id,
            CommunityReport.status == "dismissed",
        )
    ) or 0

    votes_cast = int(image_votes[0] or 0) + int(metadata_votes[0] or 0)
    positive_votes = int(image_votes[1] or 0) + int(metadata_votes[1] or 0)
    negative_votes = int(image_votes[2] or 0) + int(metadata_votes[2] or 0)

    score = 10.0 if user.email_verified else 0.0
    score += min(20.0, math.log1p(votes_cast) * 3.0)
    score += min(25.0, helpful_reports * 5.0)
    score -= min(20.0, dismissed_reports * 4.0)
    if votes_cast:
        score += ((positive_votes - negative_votes) / votes_cast) * 5.0
    return TrustScore(user_id, round(max(0.0, min(100.0, score)), 3), votes_cast,
                      positive_votes, negative_votes, int(helpful_reports), int(dismissed_reports))
