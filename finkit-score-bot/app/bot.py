from datetime import datetime
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
    from aiogram import Dispatcher, F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.filters.command import CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

    class ThresholdInputState(StatesGroup):
        waiting_for_threshold = State()

    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        settings = get_settings()
        threshold = _user_threshold(message.from_user.id if message.from_user else 0, settings)
        await message.answer(
            _build_welcome_text(settings=settings, threshold=threshold),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                threshold=threshold,
            ),
        )

    @router.message(Command("settings"))
    async def settings_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        settings = get_settings()
        user_id = message.from_user.id if message.from_user else 0
        threshold = _user_threshold(user_id, settings)
        subscriber = storage.get_subscriber(user_id)
        await message.answer(
            _build_settings_text(settings=settings, threshold=threshold, subscriber=subscriber),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                threshold=threshold,
            ),
        )

    @router.callback_query(F.data == "settings_threshold")
    async def settings_threshold(callback: CallbackQuery, state: FSMContext) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None:
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Настройка доступна только в личных сообщениях.", show_alert=True)
            return

        await state.set_state(ThresholdInputState.waiting_for_threshold)
        await message.answer("Введите порог скор балла числом от 0 до 100.")
        await callback.answer()

    @router.message(ThresholdInputState.waiting_for_threshold)
    async def save_threshold_input(message: Message, state: FSMContext) -> None:
        if not _is_private_chat(message):
            await state.clear()
            await message.answer(_private_only_text())
            return

        raw_value = (message.text or "").strip().replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            await message.answer("Введите число от 0 до 100.")
            return

        if value < 0 or value > 100:
            await message.answer("Порог должен быть в диапазоне от 0 до 100.")
            return

        user = message.from_user
        user_id = user.id if user else 0
        storage.set_user_threshold(
            user_id=user_id,
            chat_id=user_id,
            value=value,
            username=user.username if user else None,
            first_name=user.first_name if user else None,
        )
        await state.clear()

        settings = get_settings()
        subscriber = storage.get_subscriber(user_id)
        lines = [f"Порог сохранен: {_format_number(value)}."]
        if storage.is_trial_active(user_id):
            lines.append("Новое значение уже используется. Запускаю проверку по обновленному порогу.")

            from app.monitor import check_once

            try:
                offers_count, notified_count = await check_once(bot=message.bot)
                lines.append(
                    f"Проверка выполнена: найдено {offers_count}, уведомлений отправлено {notified_count}."
                )
            except Exception as exc:
                lines.append(f"Порог сохранен, но проверка завершилась ошибкой: {exc}")
        elif subscriber and subscriber.get("started_at"):
            lines.append(_build_trial_ended_text(settings))
        else:
            lines.append("Нажмите «Начать работу», чтобы запустить поиск.")

        await message.answer(
            "\n\n".join(lines)
            + "\n\n"
            + _build_settings_text(settings=settings, threshold=value, subscriber=subscriber),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                threshold=value,
            ),
        )

    @router.callback_query(F.data == "start_work")
    async def start_work(callback: CallbackQuery, state: FSMContext) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None:
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Активируйте бота в личных сообщениях.", show_alert=True)
            return

        await state.clear()
        settings = get_settings()
        activation = storage.activate_trial(
            user_id=user.id,
            chat_id=user.id,
            username=user.username,
            first_name=user.first_name,
            duration_hours=settings.trial_duration_hours,
        )
        threshold = _user_threshold(user.id, settings)

        if activation["state"] == "expired":
            await message.answer(_build_trial_ended_text(settings))
            await callback.answer()
            return

        await message.answer(
            _build_active_trial_text(
                threshold=threshold,
                settings=settings,
                subscriber=activation["subscriber"],
                just_started=activation["state"] == "started",
            ),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                threshold=threshold,
            ),
        )

        if activation["state"] == "started":
            from app.monitor import check_once

            try:
                await check_once(bot=message.bot)
            except Exception as exc:
                await message.answer(f"Поиск запущен, но первая проверка завершилась ошибкой: {exc}")
        await callback.answer()

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        settings = get_settings()
        default_threshold = storage.get_threshold(settings.default_score_threshold)
        last_check = storage.get_last_check()
        last_check_time = last_check["checked_at"] if last_check else "нет данных"
        user_id = message.from_user.id if message.from_user else 0
        admin_access = is_authorized(user_id=user_id, chat_id=message.chat.id, settings=settings)
        subscriber = storage.get_subscriber(user_id)
        user_threshold = _user_threshold(user_id, settings)

        if admin_access:
            await message.answer(
                "Статус: работает\n"
                f"Порог по умолчанию: {_format_number(default_threshold)}\n"
                f"Интервал проверки: {settings.check_interval_seconds} сек.\n"
                f"Последняя проверка: {last_check_time}"
            )
            return

        if not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        if storage.is_trial_active(user_id):
            await message.answer(
                "Статус: поиск активен\n"
                f"Ваш порог: {_format_number(user_threshold)}\n"
                f"Проверка запускается каждые {settings.check_interval_seconds} сек.\n"
                f"Доступ до: {_format_datetime(subscriber.get('expires_at') if subscriber else None)}\n"
                f"Последняя проверка: {last_check_time}"
            )
            return

        if subscriber and subscriber.get("started_at"):
            await message.answer(_build_trial_ended_text(settings))
            return

        await message.answer(
            "Поиск еще не запущен.\n"
            "Откройте /settings, настройте порог и нажмите «Начать работу»."
        )

    @router.message(Command("threshold"))
    async def threshold(message: Message, command: CommandObject) -> None:
        if not _is_admin_message(message):
            await message.answer("Команда доступна только менеджеру.")
            return

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
            f"Порог по умолчанию изменен на {_format_number(value)}.\n"
            "Запускаю повторную проверку для активных пользователей."
        )

        from app.monitor import check_after_threshold_change

        try:
            offers_count, notified_count = await check_after_threshold_change(bot=message.bot)
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
        if not _is_admin_message(message):
            await message.answer("Команда доступна только менеджеру.")
            return

        from app.monitor import check_once

        try:
            offers_count, notified_count = await check_once(bot=message.bot)
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
        if not await _ensure_user_access(message):
            return

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
    async def help_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not _is_admin_message(message) and not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        settings = get_settings()
        lines = [
            "Команды:",
            "/start - приветствие и меню",
            "/settings - настроить порог и запустить поиск",
            "/status - статус поиска и срока доступа",
            "/last - последние 5 найденных предложений",
            "/help - список команд",
        ]
        if _is_admin_message(message):
            lines.extend(
                [
                    "/threshold 70 - изменить порог по умолчанию",
                    "/check - запустить проверку вручную",
                    "/chat_id - показать ID текущего чата",
                ]
            )
        else:
            lines.append(
                f"После активации бесплатный доступ работает {settings.trial_duration_hours} часа(ов)."
            )
        await message.answer("\n".join(lines))

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def register_bot_commands(bot: Any) -> None:
    from aiogram.types import BotCommand

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="settings", description="Порог и запуск"),
            BotCommand(command="status", description="Статус доступа"),
            BotCommand(command="last", description="Последние предложения"),
            BotCommand(command="help", description="Список команд"),
        ]
    )


def _build_settings_keyboard(markup_cls: Any, button_cls: Any, *, threshold: float) -> Any:
    return markup_cls(
        inline_keyboard=[
            [button_cls(text=f"Порог: {_format_number(threshold)}", callback_data="settings_threshold")],
            [button_cls(text="Начать работу", callback_data="start_work")],
        ]
    )


def _build_welcome_text(settings: Settings, threshold: float) -> str:
    return (
        "FinKit Score Bot отслеживает новые предложения FinKit и присылает в личные сообщения те, "
        f"у которых скор балл {_comparator(settings)} {_format_number(threshold)}.\n\n"
        f"После активации вы получите {settings.trial_duration_hours} часа(ов) бесплатного доступа.\n"
        "Настройте порог в меню ниже и нажмите «Начать работу»."
    )


def _build_settings_text(
    *,
    settings: Settings,
    threshold: float,
    subscriber: dict[str, Any] | None,
) -> str:
    if subscriber and subscriber.get("started_at") and storage.is_trial_active(int(subscriber["user_id"])):
        status_text = (
            "Поиск уже активен.\n"
            f"Доступ до: {_format_datetime(subscriber.get('expires_at'))}\n"
        )
    elif subscriber and subscriber.get("started_at"):
        status_text = "Бесплатный период уже закончился.\n"
    else:
        status_text = "Поиск еще не запущен.\n"

    return (
        f"{status_text}"
        f"Текущий порог: {_format_number(threshold)}.\n"
        "Нажмите кнопку с порогом, чтобы изменить значение, затем нажмите «Начать работу»."
    )


def _build_active_trial_text(
    *,
    threshold: float,
    settings: Settings,
    subscriber: dict[str, Any] | None,
    just_started: bool,
) -> str:
    expires_at = subscriber.get("expires_at") if subscriber else None
    intro = "Поиск запущен." if just_started else "Поиск уже активен."
    return (
        f"{intro}\n\n"
        f"Сейчас бот ищет все предложения со скор баллом {_comparator(settings)} "
        f"{_format_number(threshold)} и проверяет площадку каждые "
        f"{settings.check_interval_seconds} сек.\n"
        "Как только появится подходящее предложение, бот отправит его вам в личные сообщения.\n"
        f"Доступ действует до: {_format_datetime(expires_at)}"
    )


def _build_trial_ended_text(settings: Settings) -> str:
    return (
        "Бесплатный период закончился.\n\n"
        f"Чтобы продолжить работу, напишите {settings.trial_manager_contact}."
    )


def _format_number(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_datetime(value: object) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def _comparator(settings: Settings) -> str:
    return ">=" if settings.score_compare_mode == "gte" else ">"


def _user_threshold(user_id: int, settings: Settings) -> float:
    return storage.get_user_threshold(user_id, storage.get_threshold(settings.default_score_threshold))


async def _ensure_user_access(message: Any) -> bool:
    if _is_admin_message(message):
        return True

    if not _is_private_chat(message):
        await message.answer(_private_only_text())
        return False

    user = getattr(message, "from_user", None)
    user_id = user.id if user else 0

    if storage.is_trial_active(user_id):
        return True

    subscriber = storage.get_subscriber(user_id)
    if subscriber and subscriber.get("started_at"):
        await message.answer(_build_trial_ended_text(get_settings()))
        return False

    await message.answer("Сначала откройте /settings, задайте порог и нажмите «Начать работу».")
    return False


def _is_admin_message(message: Any) -> bool:
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    user_id = user.id if user else 0
    chat_id = chat.id if chat else 0
    return is_authorized(user_id=user_id, chat_id=chat_id)


def _is_private_chat(message: Any) -> bool:
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None)
    normalized = getattr(chat_type, "value", chat_type)
    return str(normalized).lower() == "private"


def _private_only_text() -> str:
    return "Эта функция работает только в личных сообщениях с ботом. Напишите боту в ЛС и используйте /start или /settings."
