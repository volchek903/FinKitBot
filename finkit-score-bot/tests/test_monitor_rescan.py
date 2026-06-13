import asyncio
from datetime import datetime, timedelta, timezone

from app import storage
from app.config import get_settings
from app.models import Offer
from app.monitor import check_after_threshold_change, check_once


def _activate_user(user_id: int, chat_id: int | None = None) -> None:
    storage.activate_trial(
        user_id=user_id,
        chat_id=user_id if chat_id is None else chat_id,
        username=None,
        first_name=None,
        duration_hours=24,
    )


def test_regular_check_notifies_active_user_once(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()
    _activate_user(user_id=101)

    offer = Offer(id="seen-unnotified", score=60, status="available")
    notified: list[tuple[int, str]] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(
        offer: Offer,
        threshold: float,
        *,
        chat_id: int | None = None,
        bot: object | None = None,
    ) -> None:
        notified.append((chat_id or 0, offer.id))

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    offers_count, notified_count = asyncio.run(check_once())

    assert offers_count == 1
    assert notified_count == 1
    assert notified == [(101, offer.id)]
    assert storage.has_user_offer_notification(101, offer.id) is True

    offers_count, notified_count = asyncio.run(check_once())

    assert offers_count == 1
    assert notified_count == 0
    assert notified == [(101, offer.id)]


def test_regular_check_notifies_new_user_for_existing_offer(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()

    offer = Offer(id="existing-offer", score=60, status="available")
    notified: list[tuple[int, str]] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(
        offer: Offer,
        threshold: float,
        *,
        chat_id: int | None = None,
        bot: object | None = None,
    ) -> None:
        notified.append((chat_id or 0, offer.id))

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    _activate_user(user_id=101)
    asyncio.run(check_once())
    _activate_user(user_id=202)

    offers_count, notified_count = asyncio.run(check_once())

    assert offers_count == 1
    assert notified_count == 1
    assert notified == [(101, offer.id), (202, offer.id)]
    assert storage.has_user_offer_notification(202, offer.id) is True


def test_threshold_change_resends_offer_to_active_user(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()
    _activate_user(user_id=101)

    offer = Offer(id="seen-notified", score=60, status="available")
    notified: list[tuple[int, str]] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(
        offer: Offer,
        threshold: float,
        *,
        chat_id: int | None = None,
        bot: object | None = None,
    ) -> None:
        notified.append((chat_id or 0, offer.id))

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    asyncio.run(check_once())
    offers_count, notified_count = asyncio.run(check_after_threshold_change())

    assert offers_count == 1
    assert notified_count == 1
    assert notified == [(101, offer.id), (101, offer.id)]


def test_group_activation_does_not_receive_private_notifications(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    monkeypatch.setenv("DEFAULT_SCORE_THRESHOLD", "50")
    storage.init_db()
    _activate_user(user_id=101, chat_id=-100123)

    offer = Offer(id="group-activation", score=60, status="available")
    notified: list[tuple[int, str]] = []

    async def fake_get_offers() -> list[Offer]:
        return [offer]

    async def fake_notify_offer(
        offer: Offer,
        threshold: float,
        *,
        chat_id: int | None = None,
        bot: object | None = None,
    ) -> None:
        notified.append((chat_id or 0, offer.id))

    monkeypatch.setattr("app.finkit_client.get_offers", fake_get_offers)
    monkeypatch.setattr("app.notifier.notify_offer", fake_notify_offer)

    offers_count, notified_count = asyncio.run(check_once())

    assert offers_count == 0
    assert notified_count == 0
    assert notified == []
    assert storage.is_trial_active(101) is False


def test_trial_starts_once_and_does_not_restart(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    storage.init_db()

    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.storage._utcnow", lambda: started_at)

    first = storage.activate_trial(
        user_id=10,
        chat_id=10,
        username="user",
        first_name="Test",
        duration_hours=24,
    )

    assert first["state"] == "started"
    assert storage.is_trial_active(10) is True

    after_expiry = started_at + timedelta(hours=25)
    monkeypatch.setattr("app.storage._utcnow", lambda: after_expiry)

    second = storage.activate_trial(
        user_id=10,
        chat_id=10,
        username="user2",
        first_name="Test2",
        duration_hours=24,
    )

    subscriber = storage.get_subscriber(10)
    assert second["state"] == "expired"
    assert storage.is_trial_active(10) is False
    assert subscriber["chat_id"] == 10
    assert subscriber["started_at"] == started_at.isoformat(timespec="seconds")
    assert subscriber["expires_at"] == (started_at + timedelta(hours=24)).isoformat(
        timespec="seconds"
    )


def test_expired_subscriber_pending_notice_is_marked(monkeypatch, tmp_path) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.sqlite3"))
    storage.init_db()

    started_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.storage._utcnow", lambda: started_at)
    storage.activate_trial(
        user_id=10,
        chat_id=10,
        username=None,
        first_name=None,
        duration_hours=24,
    )

    expired_at = started_at + timedelta(hours=25)
    monkeypatch.setattr("app.storage._utcnow", lambda: expired_at)

    pending = storage.get_expired_subscribers_pending_notice()
    assert [row["user_id"] for row in pending] == [10]

    storage.mark_trial_ended_notified(10)

    assert storage.get_expired_subscribers_pending_notice() == []
