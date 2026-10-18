from app.trust import TrustScore


def test_trust_score_is_bounded_and_structured():
    score = TrustScore(1, 42.5, 10, 8, 2, 3, 0)
    assert 0 <= score.score <= 100
    assert score.votes_cast == score.positive_votes + score.negative_votes
