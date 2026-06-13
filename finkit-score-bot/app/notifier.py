import asyncio
import logging
from typing import Any

from app.config import get_settings
from app.models import Offer
from app.offers_url import build_offers_url

logger = logging.getLogger(__name__)


async def notify_offer(
    offer: Offer,
    threshold: float,
    *,
    chat_id: int | None = None,
    bot: Any | None = None,
) -> None:
    settings = get_settings()
    comparator = ">=" if settings.score_compare_mode == "gte" else ">"
    text = (
        "🟢 Новое предложение FinKit\n\n"
        f"Скор балл: {_fmt(offer.score)}\n"
        f"Порог: {comparator} {_fmt(threshold)}\n\n"
        f"Сумма: {_fmt(offer.amount)}\n"
        f"Срок: {_fmt(offer.term)} дней\n"
        f"Ставка: {_fmt(offer.rate)}\n"
        f"Рейтинг: {_fmt(offer.rating)}\n"
        f"Заемщик: {_fmt(offer.borrower)}\n"
        f"Дата размещения: {_fmt(offer.signed_at)}\n"
        f"Ожидаемый доход: {_fmt(offer.expected_income)}\n"
        f"Статус: {_fmt(offer.status)}\n\n"
        "Ссылка:\n"
        f"{_offer_link(offer, threshold, settings.finkit_offers_url)}"
    )
    await _send_text(text, chat_id=chat_id, bot=bot)


async def notify_trial_ended(
    *,
    chat_id: int,
    manager_contact: str,
    bot: Any | None = None,
) -> None:
    text = (
        "Бесплатный период закончился.\n\n"
        f"Чтобы продолжить работу, напишите {manager_contact}."
    )
    await _send_text(text, chat_id=chat_id, bot=bot, fail_silently=True)


async def notify_admin_error(title: str, error: str) -> None:
    text = f"{title}\n\n{error}"
    try:
        await _send_text(text[:4000], fail_silently=True)
    except Exception:
        logger.exception("failed to notify admin")


async def _send_text(
    text: str,
    *,
    chat_id: int | None = None,
    bot: Any | None = None,
    fail_silently: bool = False,
) -> None:
    settings = get_settings()
    target_chat_id = chat_id if chat_id is not None else settings.telegram_chat_id_int
    if not settings.telegram_bot_token or target_chat_id is None:
        message = "Telegram credentials are not configured"
        if fail_silently:
            logger.warning(message)
            return
        raise RuntimeError(message)

    from aiogram import Bot as TelegramBot
    from aiogram.exceptions import TelegramRetryAfter

    telegram_bot = bot or TelegramBot(token=settings.telegram_bot_token)
    should_close_session = bot is None
    try:
        while True:
            try:
                await telegram_bot.send_message(
                    chat_id=target_chat_id,
                    text=text,
                    disable_web_page_preview=True,
                )
                return
            except TelegramRetryAfter as exc:
                logger.warning("telegram flood control hit retry_after=%s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
    finally:
        if should_close_session:
            await telegram_bot.session.close()


def _fmt(value: object) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _offer_link(offer: Offer, threshold: float, default_url: str) -> str:
    if offer.url:
        return offer.url
    return build_offers_url(default_url, threshold)
