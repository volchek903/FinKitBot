from app.models import Offer
from app.monitor import is_available, score_matches


def test_threshold_gt_mode() -> None:
    assert score_matches(66, 65, "gt") is True
    assert score_matches(65, 65, "gt") is False
    assert score_matches(None, 65, "gt") is False


def test_threshold_gte_mode() -> None:
    assert score_matches(65, 65, "gte") is True
    assert score_matches(64.9, 65, "gte") is False


def test_is_available() -> None:
    assert is_available(Offer(id="1", score=70, status=None)) is True
    assert is_available(Offer(id="2", score=70, status="unknown")) is True
    assert is_available(Offer(id="3", score=70, status="Инвестировать")) is True
    assert is_available(Offer(id="4", score=70, status="available")) is True
    assert is_available(Offer(id="5", score=70, status="closed")) is False
    assert is_available(Offer(id="6", score=70, status="недоступно")) is False

