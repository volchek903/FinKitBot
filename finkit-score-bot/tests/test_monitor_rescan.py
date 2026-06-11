import asyncio

from app import storage
from app.config import get_settings
from app.models import Offer
from app.monitor import check_after_threshold_change, check_once


def test_regular_check_skips_seen_unnotified_offer(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()

    offer = Offer(id="seen-unnotified", score=60, status="available")
    storage.save_seen(offer)
    notified: list[str] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(offer: Offer, threshold: float) -> None:
        notified.append(offer.id)

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    offers_count, notified_count = asyncio.run(check_once())

    assert offers_count == 1
    assert notified_count == 0
    assert notified == []
    assert storage.get_seen(offer.id)["notified_at"] is None


def test_threshold_rescan_notifies_seen_unnotified_offer_once(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()

    offer = Offer(id="seen-unnotified", score=60, status="available")
    storage.save_seen(offer)
    notified: list[str] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(offer: Offer, threshold: float) -> None:
        notified.append(offer.id)

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    offers_count, notified_count = asyncio.run(check_once(include_seen_unnotified=True))

    assert offers_count == 1
    assert notified_count == 1
    assert notified == [offer.id]
    assert storage.get_seen(offer.id)["notified_at"] is not None

    offers_count, notified_count = asyncio.run(check_once(include_seen_unnotified=True))

    assert offers_count == 1
    assert notified_count == 0
    assert notified == [offer.id]


def test_threshold_change_resends_seen_notified_offer(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()

    offer = Offer(id="seen-notified", score=60, status="available")
    storage.save_seen(offer)
    storage.mark_notified(offer.id)
    notified: list[str] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(offer: Offer, threshold: float) -> None:
        notified.append(offer.id)

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    offers_count, notified_count = asyncio.run(check_after_threshold_change())

    assert offers_count == 1
    assert notified_count == 1
    assert notified == [offer.id]
