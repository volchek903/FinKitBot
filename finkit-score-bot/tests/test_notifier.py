import asyncio

import aiogram
import aiogram.exceptions

from app.config import get_settings
from app.models import Offer
from app.notifier import _offer_link, _send_text


class FakeRetryAfter(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakeBot:
    attempts = 0
    instances: list["FakeBot"] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = FakeSession()
        self.sent_messages: list[dict[str, object]] = []
        FakeBot.instances.append(self)

    async def send_message(self, **kwargs: object) -> None:
        FakeBot.attempts += 1
        if FakeBot.attempts == 1:
            raise FakeRetryAfter(2)
        self.sent_messages.append(kwargs)


def test_send_text_retries_after_telegram_flood_control(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    get_settings.cache_clear()

    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    FakeBot.attempts = 0
    FakeBot.instances = []

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(aiogram.exceptions, "TelegramRetryAfter", FakeRetryAfter)
    monkeypatch.setattr("app.notifier.asyncio.sleep", fake_sleep)

    asyncio.run(_send_text("hello"))

    assert FakeBot.attempts == 2
    assert sleeps == [2]
    assert len(FakeBot.instances) == 1
    assert FakeBot.instances[0].sent_messages == [
        {"chat_id": 12345, "text": "hello", "disable_web_page_preview": True}
    ]
    assert FakeBot.instances[0].session.closed == 1


def test_offer_link_uses_offer_url_when_present() -> None:
    offer = Offer(id="1", score=50, url="https://finkit.by/app/invest-manually/offer/1")

    link = _offer_link(offer, threshold=44, default_url="https://finkit.by/app/invest-manually")

    assert link == "https://finkit.by/app/invest-manually/offer/1"


def test_offer_link_rewrites_threshold_in_default_url() -> None:
    offer = Offer(id="1", score=50, url=None)

    link = _offer_link(
        offer,
        threshold=44,
        default_url="https://finkit.by/app/invest-manually?borrower_score_min=30&ordering=-signed_at",
    )

    assert link == (
        "https://finkit.by/app/invest-manually"
        "?borrower_score_min=44&ordering=-signed_at"
    )
