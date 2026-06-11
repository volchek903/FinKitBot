from typing import Any

from app import storage
from app.config import Settings, get_settings


def is_authorized(user_id: int, chat_id: int, settings: Settings | None = None) -> bool:
    current_settings = settings or get_settings()
    if current_settings.telegram_allowed_user_ids:
        return user_id in current_settings.telegram_allowed_user_ids

    allowed_chat_id = current_settings.telegram_chat_id_int
    if allowed_chat_id is None:
        return False
    return user_id == allowed_chat_id or chat_id == allowed_chat_id


def is_explicitly_allowed_user(user_id: int, settings: Settings | None = None) -> bool:
    current_settings = settings or get_settings()
    return bool(current_settings.telegram_allowed_user_ids) and (
        user_id in current_settings.telegram_allowed_user_ids
    )


def create_dispatcher() -> Any:
    from aiogram import Dispatcher, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.filters.command import CommandObject
    from aiogram.types import Message

    router = Router()

    async def deny(message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        if is_authorized(user_id=user_id, chat_id=message.chat.id):
            return True
        if _is_chat_id_command(message):
            return False
        await message.answer("Доступ запрещен.")
        return False

    router.message.filter(deny)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        settings = get_settings()
        threshold = storage.get_threshold(settings.default_score_threshold)
        await message.answer(
            "Бот мониторит FinKit и уведомляет о новых предложениях "
            "со скор баллом не ниже порога.\n\n"
            f"Текущий порог: {_format_number(threshold)}\n"
            f"Интервал проверки: {settings.check_interval_seconds} сек."
        )

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        settings = get_settings()
        threshold = storage.get_threshold(settings.default_score_threshold)
        last_check = storage.get_last_check()
        last_check_time = last_check["checked_at"] if last_check else "нет данных"
        await message.answer(
            "Статус: работает\n"
            f"Текущий порог: {_format_number(threshold)}\n"
            f"Интервал проверки: {settings.check_interval_seconds} сек.\n"
            f"Последняя проверка: {last_check_time}"
        )

    @router.message(Command("threshold"))
    async def threshold(message: Message, command: CommandObject) -> None:
        raw_value = (command.args or "").strip().replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            await message.answer("Значение порога должно быть числом.")
            return

        if value < 0 or value > 100:
            await message.answer("Порог должен быть в диапазоне 0 <= threshold <= 100.")
            return

        storage.set_threshold(value)
        await message.answer(
            f"Порог скор балла изменен на {_format_number(value)}.\n"
            "Запускаю проверку по новому порогу."
        )

        from app.monitor import check_after_threshold_change

        try:
            offers_count, notified_count = await check_after_threshold_change()
        except Exception as exc:
            await message.answer(f"Порог сохранен, но проверка завершилась ошибкой: {exc}")
            return

        await message.answer(
            "Проверка по новому порогу выполнена.\n"
            f"Найдено предложений: {offers_count}\n"
            f"Уведомлений отправлено: {notified_count}"
        )

    @router.message(Command("check"))
    async def check(message: Message) -> None:
        from app.monitor import check_once

        try:
            offers_count, notified_count = await check_once()
        except Exception as exc:
            await message.answer(f"Проверка завершилась ошибкой: {exc}")
            return

        await message.answer(
            "Проверка выполнена.\n"
            f"Найдено предложений: {offers_count}\n"
            f"Уведомлений отправлено: {notified_count}"
        )

    @router.message(Command("last"))
    async def last(message: Message) -> None:
        offers = storage.get_recent_offers(limit=5)
        if not offers:
            await message.answer("Сохраненных предложений пока нет.")
            return

        lines = ["Последние сохраненные предложения:"]
        for offer in offers:
            lines.append(
                "\n"
                f"ID: {offer['offer_id']}\n"
                f"Скор балл: {_format_number(offer['score'])}\n"
                f"Первое появление: {offer['first_seen_at']}\n"
                f"Уведомление: {offer['notified_at'] or '-'}\n"
                f"Ссылка: {offer['url'] or '-'}"
            )
        await message.answer("\n".join(lines), disable_web_page_preview=True)

    @router.message(Command("chat_id"))
    async def chat_id(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_explicitly_allowed_user(user_id=user_id):
            return

        await message.answer(
            f"TELEGRAM_CHAT_ID={message.chat.id}\n"
            f"TELEGRAM_ALLOWED_USER_IDS={user_id}"
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Команды:\n"
            "/start - краткое описание\n"
            "/status - состояние мониторинга\n"
            "/threshold 70 - изменить порог скор балла\n"
            "/check - запустить проверку вручную\n"
            "/last - последние 5 сохраненных предложений\n"
            "/chat_id - показать ID текущего чата\n"
            "/help - список команд"
        )

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def register_bot_commands(bot: Any) -> None:
    from aiogram.types import BotCommand

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Описание бота"),
            BotCommand(command="status", description="Статус мониторинга"),
            BotCommand(command="threshold", description="Изменить порог"),
            BotCommand(command="check", description="Проверить сейчас"),
            BotCommand(command="last", description="Последние предложения"),
            BotCommand(command="chat_id", description="Показать ID чата"),
            BotCommand(command="help", description="Список команд"),
        ]
    )


def _format_number(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _is_chat_id_command(message: Any) -> bool:
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    first_token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    command = first_token.split("@", maxsplit=1)[0].lower()
    return command == "/chat_id"
