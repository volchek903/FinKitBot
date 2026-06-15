import asyncio
import logging
from dataclasses import dataclass
from time import time
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.models import Offer
from app.offers_url import build_offers_url

logger = logging.getLogger(__name__)
OFFER_BATCH_PAGE_SIZE = 10
OFFER_BATCH_CALLBACK_PREFIX = "offer_batch:"
MAX_OFFER_BATCH_SESSIONS = 100
TELEGRAM_NETWORK_RETRIES = 3
TELEGRAM_NETWORK_RETRY_DELAY_SECONDS = 2


@dataclass
class OfferBatchSession:
    session_id: str
    chat_id: int
    threshold: float
    filters: dict[str, Any]
    offers: list[Offer]
    page_message_ids: list[int]
    navigation_message_id: int | None
    created_at: float


def create_telegram_bot(*, token: str | None = None) -> Any:
    settings = get_settings()
    bot_token = token or settings.telegram_bot_token
    if not bot_token:
        raise RuntimeError("Telegram bot token is not configured")

    from aiogram import Bot as TelegramBot
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(proxy=settings.telegram_proxy or None)
    return TelegramBot(token=bot_token, session=session)


async def notify_offer(
    offer: Offer,
    threshold: float,
    *,
    chat_id: int | None = None,
    filters: dict[str, Any] | None = None,
    bot: Any | None = None,
) -> Any:
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
        f"{_offer_link(offer, threshold, settings.finkit_offers_url, filters=filters)}"
    )
    return await _send_text(text, chat_id=chat_id, bot=bot)


async def notify_offer_batch(
    offers: list[Offer],
    threshold: float,
    *,
    chat_id: int,
    filters: dict[str, Any] | None = None,
    bot: Any | None = None,
) -> Any:
    if not offers:
        return None
    if len(offers) <= OFFER_BATCH_PAGE_SIZE:
        message = None
        for offer in offers:
            message = await notify_offer(
                offer,
                threshold,
                chat_id=chat_id,
                filters=filters,
                bot=bot,
            )
        return message

    session = _remember_offer_batch_session(
        chat_id=chat_id,
        threshold=threshold,
        filters=filters or {},
        offers=offers,
    )
    return await _send_offer_batch_page(
        session,
        page_index=0,
        bot=bot,
    )


async def show_offer_batch_page(
    session_id: str,
    page_index: int,
    *,
    chat_id: int,
    bot: Any,
) -> tuple[int, int]:
    session = _offer_batch_sessions.get(session_id)
    if session is None or session.chat_id != chat_id:
        raise KeyError(session_id)

    _prune_offer_batch_sessions()
    total_pages = _offer_batch_total_pages(len(session.offers))
    safe_page_index = max(0, min(page_index, total_pages - 1))
    await _delete_offer_batch_page_messages(session, bot=bot)
    await _send_offer_batch_page(
        session,
        page_index=safe_page_index,
        bot=bot,
    )
    return safe_page_index + 1, total_pages


def parse_offer_batch_callback_data(data: str) -> tuple[str, int] | None:
    if not data.startswith(OFFER_BATCH_CALLBACK_PREFIX):
        return None
    try:
        _, session_id, raw_page_index = data.split(":", maxsplit=2)
    except ValueError:
        return None
    try:
        return session_id, int(raw_page_index)
    except ValueError:
        return None


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
    except Exception as exc:
        logger.warning("failed to notify admin: %s", exc)


async def copy_message_to_chat(
    *,
    source_chat_id: int,
    message_id: int,
    chat_id: int,
    bot: Any | None = None,
    fail_silently: bool = False,
) -> Any:
    settings = get_settings()
    if not settings.telegram_bot_token:
        message = "Telegram bot token is not configured"
        if fail_silently:
            logger.warning(message)
            return
        raise RuntimeError(message)

    from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

    telegram_bot = bot or create_telegram_bot(token=settings.telegram_bot_token)
    should_close_session = bot is None
    network_error_attempt = 0
    network_retry_delay = TELEGRAM_NETWORK_RETRY_DELAY_SECONDS
    try:
        while True:
            try:
                return await telegram_bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=message_id,
                )
            except TelegramRetryAfter as exc:
                logger.warning("telegram flood control hit retry_after=%s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                network_error_attempt += 1
                if network_error_attempt >= TELEGRAM_NETWORK_RETRIES:
                    if fail_silently:
                        logger.warning(
                            "telegram network error after %s attempts: %s",
                            network_error_attempt,
                            exc,
                        )
                        return
                    raise
                logger.warning(
                    "telegram network error attempt=%s/%s retry_in=%ss error=%s",
                    network_error_attempt,
                    TELEGRAM_NETWORK_RETRIES,
                    network_retry_delay,
                    exc,
                )
                await asyncio.sleep(network_retry_delay)
                network_retry_delay = min(30, network_retry_delay * 2)
    finally:
        if should_close_session:
            await telegram_bot.session.close()


async def _send_text(
    text: str,
    *,
    chat_id: int | None = None,
    bot: Any | None = None,
    reply_markup: Any | None = None,
    fail_silently: bool = False,
) -> Any:
    settings = get_settings()
    target_chat_id = chat_id if chat_id is not None else settings.telegram_chat_id_int
    if not settings.telegram_bot_token or target_chat_id is None:
        message = "Telegram credentials are not configured"
        if fail_silently:
            logger.warning(message)
            return
        raise RuntimeError(message)

    from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

    telegram_bot = bot or create_telegram_bot(token=settings.telegram_bot_token)
    should_close_session = bot is None
    network_error_attempt = 0
    network_retry_delay = TELEGRAM_NETWORK_RETRY_DELAY_SECONDS
    try:
        while True:
            try:
                kwargs = {
                    "chat_id": target_chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }
                if reply_markup is not None:
                    kwargs["reply_markup"] = reply_markup
                return await telegram_bot.send_message(**kwargs)
            except TelegramRetryAfter as exc:
                logger.warning("telegram flood control hit retry_after=%s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                network_error_attempt += 1
                if network_error_attempt >= TELEGRAM_NETWORK_RETRIES:
                    if fail_silently:
                        logger.warning("telegram network error after %s attempts: %s", network_error_attempt, exc)
                        return
                    raise
                logger.warning(
                    "telegram network error attempt=%s/%s retry_in=%ss error=%s",
                    network_error_attempt,
                    TELEGRAM_NETWORK_RETRIES,
                    network_retry_delay,
                    exc,
                )
                await asyncio.sleep(network_retry_delay)
                network_retry_delay = min(30, network_retry_delay * 2)
    finally:
        if should_close_session:
            await telegram_bot.session.close()


def _fmt(value: object) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _offer_link(
    offer: Offer,
    threshold: float,
    default_url: str,
    *,
    filters: dict[str, Any] | None = None,
) -> str:
    if offer.url:
        return offer.url
    return build_offers_url(default_url, threshold, filters=filters)


def _offer_batch_prompt_text(session: OfferBatchSession, page_index: int) -> str:
    start = page_index * OFFER_BATCH_PAGE_SIZE
    end = min(start + OFFER_BATCH_PAGE_SIZE, len(session.offers))
    if end < len(session.offers):
        return (
            f"Показаны новые предложения {start + 1}-{end} из {len(session.offers)}.\n"
            "Хотите просмотреть следующие предложения?"
        )
    if start > 0:
        return (
            f"Показаны новые предложения {start + 1}-{end} из {len(session.offers)}.\n"
            "Это последняя страница. Можно вернуться к предыдущим."
        )
    return f"Показаны новые предложения {start + 1}-{end} из {len(session.offers)}."


def _offer_batch_keyboard(session_id: str, page_index: int, offers_count: int) -> Any:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    total_pages = _offer_batch_total_pages(offers_count)
    buttons: list[InlineKeyboardButton] = []
    if page_index > 0:
        buttons.append(
            InlineKeyboardButton(
                text="Предыдущие 10",
                callback_data=f"{OFFER_BATCH_CALLBACK_PREFIX}{session_id}:{page_index - 1}",
            )
        )
    if page_index < total_pages - 1:
        buttons.append(
            InlineKeyboardButton(
                text="Показать следующие 10",
                callback_data=f"{OFFER_BATCH_CALLBACK_PREFIX}{session_id}:{page_index + 1}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons] if buttons else [])


def _offer_batch_total_pages(offers_count: int) -> int:
    return max(1, (offers_count + OFFER_BATCH_PAGE_SIZE - 1) // OFFER_BATCH_PAGE_SIZE)


def _remember_offer_batch_session(
    *,
    chat_id: int,
    threshold: float,
    filters: dict[str, Any],
    offers: list[Offer],
) -> OfferBatchSession:
    _prune_offer_batch_sessions()
    session = OfferBatchSession(
        session_id=uuid4().hex[:8],
        chat_id=chat_id,
        threshold=threshold,
        filters=dict(filters),
        offers=list(offers),
        page_message_ids=[],
        navigation_message_id=None,
        created_at=time(),
    )
    _offer_batch_sessions[session.session_id] = session
    return session


async def _send_offer_batch_page(
    session: OfferBatchSession,
    *,
    page_index: int,
    bot: Any,
) -> Any:
    start = page_index * OFFER_BATCH_PAGE_SIZE
    end = min(start + OFFER_BATCH_PAGE_SIZE, len(session.offers))
    page_message_ids: list[int] = []

    for offer in session.offers[start:end]:
        message = await notify_offer(
            offer,
            session.threshold,
            chat_id=session.chat_id,
            filters=session.filters,
            bot=bot,
        )
        message_id = getattr(message, "message_id", None)
        if message_id is None and isinstance(message, dict):
            message_id = message.get("message_id")
        if isinstance(message_id, int):
            page_message_ids.append(message_id)

    navigation_message = await _send_text(
        _offer_batch_prompt_text(session, page_index),
        chat_id=session.chat_id,
        bot=bot,
        reply_markup=_offer_batch_keyboard(
            session.session_id,
            page_index=page_index,
            offers_count=len(session.offers),
        ),
    )
    navigation_message_id = getattr(navigation_message, "message_id", None)
    if navigation_message_id is None and isinstance(navigation_message, dict):
        navigation_message_id = navigation_message.get("message_id")

    session.page_message_ids = page_message_ids
    session.navigation_message_id = (
        int(navigation_message_id) if isinstance(navigation_message_id, int) else None
    )
    return navigation_message


async def _delete_offer_batch_page_messages(session: OfferBatchSession, *, bot: Any) -> None:
    message_ids = list(session.page_message_ids)
    if session.navigation_message_id is not None:
        message_ids.append(session.navigation_message_id)
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=session.chat_id, message_id=message_id)
        except Exception:
            logger.debug("failed to delete offer batch message message_id=%s", message_id)
    session.page_message_ids = []
    session.navigation_message_id = None


def _prune_offer_batch_sessions() -> None:
    expired_before = time() - 24 * 60 * 60
    expired_session_ids = [
        session_id
        for session_id, session in _offer_batch_sessions.items()
        if session.created_at < expired_before
    ]
    for session_id in expired_session_ids:
        _offer_batch_sessions.pop(session_id, None)

    if len(_offer_batch_sessions) <= MAX_OFFER_BATCH_SESSIONS:
        return

    for session_id, _ in sorted(
        _offer_batch_sessions.items(),
        key=lambda item: item[1].created_at,
    )[: len(_offer_batch_sessions) - MAX_OFFER_BATCH_SESSIONS]:
        _offer_batch_sessions.pop(session_id, None)


_offer_batch_sessions: dict[str, OfferBatchSession] = {}
