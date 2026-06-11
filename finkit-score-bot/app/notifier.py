import logging

from app.config import get_settings
from app.models import Offer

logger = logging.getLogger(__name__)


async def notify_offer(offer: Offer, threshold: float) -> None:
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
        f"{offer.url or settings.finkit_offers_url}"
    )
    await _send_text(text)


async def notify_admin_error(title: str, error: str) -> None:
    text = f"{title}\n\n{error}"
    try:
        await _send_text(text[:4000], fail_silently=True)
    except Exception:
        logger.exception("failed to notify admin")


async def _send_text(text: str, fail_silently: bool = False) -> None:
    settings = get_settings()
    chat_id = settings.telegram_chat_id_int
    if not settings.telegram_bot_token or chat_id is None:
        message = "Telegram credentials are not configured"
        if fail_silently:
            logger.warning(message)
            return
        raise RuntimeError(message)

    from aiogram import Bot as TelegramBot

    bot = TelegramBot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    finally:
        await bot.session.close()


def _fmt(value: object) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)

