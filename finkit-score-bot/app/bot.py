import asyncio
import logging
from datetime import datetime
from datetime import timezone
from typing import Any

from app import storage
from app.config import Settings, get_settings
from app.user_filters import (
    FILTER_FIELD_MAP,
    empty_user_filters,
    filter_button_text,
    filter_field_label,
    filter_prompt,
    format_filter_value,
    resolved_user_filters,
    validate_filter_value,
)

FILTER_BUTTON_ROWS: tuple[tuple[str, ...], ...] = (
    ("borrower_score_min", "borrower_score_max"),
    ("amount_min", "amount_max"),
    ("term_min", "term_max"),
    ("interest_rate_min", "interest_rate_max"),
    ("borrower_rating_min", "borrower_rating_max"),
    ("invest_min", "invest_max"),
    ("borrower_income_confirmed", "borrower_enforcement_up_to_1_month_absent"),
    ("borrower_age_group",),
)
ADMIN_CALLBACK_PREFIX = "admin:"
ADMIN_USERS_PAGE_SIZE = 10
ADMIN_NOOP_CALLBACK = f"{ADMIN_CALLBACK_PREFIX}noop"
COMMAND_SYNC_RETRIES = 3
COMMAND_SYNC_RETRY_DELAY_SECONDS = 2

logger = logging.getLogger(__name__)


def is_authorized(user_id: int, chat_id: int, settings: Settings | None = None) -> bool:
    del chat_id
    return is_explicitly_allowed_user(user_id=user_id, settings=settings)


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

    class FilterInputState(StatesGroup):
        waiting_for_value = State()

    class AdminInputState(StatesGroup):
        waiting_broadcast_all_message = State()
        waiting_broadcast_target_id = State()
        waiting_broadcast_target_message = State()

    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        user_id = message.from_user.id if message.from_user else 0
        search_was_running = await storage.apause_user_search(user_id)
        settings = get_settings()
        filters = await _resolved_filters(user_id, settings)
        await message.answer(
            _build_welcome_text(
                settings=settings,
                filters=filters,
                search_was_stopped=search_was_running,
            ),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                filters=filters,
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
        filters, subscriber = await asyncio.gather(
            _resolved_filters(user_id, settings),
            storage.aget_subscriber(user_id),
        )
        await message.answer(
            _build_settings_text(
                settings=settings,
                filters=filters,
                subscriber=subscriber,
                is_trial_running=_subscriber_trial_is_running(subscriber),
                is_trial_active=_subscriber_trial_is_active(subscriber),
            ),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                filters=filters,
            ),
        )

    @router.message(Command("admin"))
    async def admin_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = message.from_user
        if user is None or not is_explicitly_allowed_user(user.id):
            return
        if not _is_private_chat(message):
            await message.answer("🔒 Админка доступна только в личных сообщениях.")
            return

        await message.answer(
            await _build_admin_home_text(),
            reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
        )

    @router.callback_query(F.data.startswith(ADMIN_CALLBACK_PREFIX))
    async def admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None or not is_explicitly_allowed_user(user.id):
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Админка доступна только в личных сообщениях.", show_alert=True)
            return

        data = callback.data or ""
        if data == ADMIN_NOOP_CALLBACK:
            await callback.answer()
            return

        if data in {f"{ADMIN_CALLBACK_PREFIX}menu", f"{ADMIN_CALLBACK_PREFIX}cancel"}:
            await state.clear()
            await _edit_message_text(
                message,
                text=await _build_admin_home_text(),
                reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
            )
            await callback.answer()
            return

        if data == f"{ADMIN_CALLBACK_PREFIX}stats":
            await state.clear()
            stats = await storage.aget_user_stats()
            await _edit_message_text(
                message,
                text=_build_admin_stats_text(stats),
                reply_markup=_build_admin_back_keyboard(
                    InlineKeyboardMarkup,
                    InlineKeyboardButton,
                ),
            )
            await callback.answer("📊 Обновлено")
            return

        if data == f"{ADMIN_CALLBACK_PREFIX}users":
            await state.clear()
            current_page, total_pages, text, reply_markup = await _build_admin_users_page(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                page_index=0,
            )
            await _edit_message_text(message, text=text, reply_markup=reply_markup)
            await callback.answer(f"Страница {current_page} из {total_pages}")
            return

        if data.startswith(f"{ADMIN_CALLBACK_PREFIX}users:"):
            await state.clear()
            raw_page_index = data.split(":", maxsplit=2)[2]
            try:
                page_index = int(raw_page_index)
            except ValueError:
                await callback.answer("⚠️ Не удалось открыть страницу.", show_alert=True)
                return

            current_page, total_pages, text, reply_markup = await _build_admin_users_page(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                page_index=page_index,
            )
            await _edit_message_text(message, text=text, reply_markup=reply_markup)
            await callback.answer(f"Страница {current_page} из {total_pages}")
            return

        if data == f"{ADMIN_CALLBACK_PREFIX}broadcast_all":
            await state.clear()
            await state.set_state(AdminInputState.waiting_broadcast_all_message)
            user_ids = await storage.aget_private_user_ids()
            await _edit_message_text(
                message,
                text=_build_admin_broadcast_all_prompt(len(user_ids)),
                reply_markup=_build_admin_cancel_keyboard(
                    InlineKeyboardMarkup,
                    InlineKeyboardButton,
                ),
            )
            await callback.answer()
            return

        if data == f"{ADMIN_CALLBACK_PREFIX}broadcast_user":
            await state.clear()
            await state.set_state(AdminInputState.waiting_broadcast_target_id)
            await _edit_message_text(
                message,
                text=(
                    "👤 Введите Telegram ID пользователя.\n\n"
                    "После этого я попрошу переслать текст или медиа для отправки."
                ),
                reply_markup=_build_admin_cancel_keyboard(
                    InlineKeyboardMarkup,
                    InlineKeyboardButton,
                ),
            )
            await callback.answer()
            return

        await callback.answer()

    @router.message(AdminInputState.waiting_broadcast_target_id)
    async def admin_broadcast_target_id(message: Message, state: FSMContext) -> None:
        if await _handle_admin_escape_command(
            message,
            state,
            InlineKeyboardMarkup,
            InlineKeyboardButton,
        ):
            return
        if not _is_admin_message(message):
            await state.clear()
            return
        if not _is_private_chat(message):
            await message.answer("🔒 Админка доступна только в личных сообщениях.")
            return

        raw_value = (message.text or "").strip()
        try:
            target_user_id = int(raw_value)
        except ValueError:
            await message.answer(
                "⚠️ Нужен числовой Telegram ID. Пример: `123456789`.",
                reply_markup=_build_admin_cancel_keyboard(
                    InlineKeyboardMarkup,
                    InlineKeyboardButton,
                ),
            )
            return

        await state.update_data(admin_target_user_id=target_user_id)
        await state.set_state(AdminInputState.waiting_broadcast_target_message)

        subscriber = await storage.aget_subscriber(target_user_id)
        await message.answer(
            _build_admin_target_prompt(target_user_id, subscriber),
            reply_markup=_build_admin_cancel_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
            ),
        )

    @router.message(AdminInputState.waiting_broadcast_all_message)
    async def admin_broadcast_all(message: Message, state: FSMContext) -> None:
        if await _handle_admin_escape_command(
            message,
            state,
            InlineKeyboardMarkup,
            InlineKeyboardButton,
        ):
            return
        if not _is_admin_message(message):
            await state.clear()
            return
        if not _is_private_chat(message):
            await message.answer("🔒 Админка доступна только в личных сообщениях.")
            return

        user_ids = await storage.aget_private_user_ids()
        if not user_ids:
            await state.clear()
            await message.answer(
                "📭 В базе пока нет пользователей для рассылки.",
                reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
            )
            return

        sent_count, failed_ids = await _broadcast_message_copy(message, user_ids)
        await state.clear()
        await message.answer(
            _build_admin_broadcast_result_text(
                total_recipients=len(user_ids),
                sent_count=sent_count,
                failed_ids=failed_ids,
            ),
            reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
        )

    @router.message(AdminInputState.waiting_broadcast_target_message)
    async def admin_broadcast_target_message(message: Message, state: FSMContext) -> None:
        if await _handle_admin_escape_command(
            message,
            state,
            InlineKeyboardMarkup,
            InlineKeyboardButton,
        ):
            return
        if not _is_admin_message(message):
            await state.clear()
            return
        if not _is_private_chat(message):
            await message.answer("🔒 Админка доступна только в личных сообщениях.")
            return

        state_data = await state.get_data()
        target_user_id = state_data.get("admin_target_user_id")
        if not isinstance(target_user_id, int):
            await state.clear()
            await message.answer(
                "⚠️ Не удалось определить пользователя. Откройте /admin и повторите отправку.",
                reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
            )
            return

        sent_count, failed_ids = await _broadcast_message_copy(message, [target_user_id])
        await state.clear()
        await message.answer(
            _build_admin_single_broadcast_result_text(
                target_user_id=target_user_id,
                sent_count=sent_count,
                failed_ids=failed_ids,
            ),
            reply_markup=_build_admin_keyboard(InlineKeyboardMarkup, InlineKeyboardButton),
        )

    @router.callback_query(F.data.startswith("edit_filter:"))
    async def edit_filter(callback: CallbackQuery, state: FSMContext) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None:
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Настройка доступна только в личных сообщениях.", show_alert=True)
            return

        field_key = (callback.data or "").split(":", maxsplit=1)[1]
        if field_key not in FILTER_FIELD_MAP:
            await callback.answer("Неизвестное поле.", show_alert=True)
            return

        await callback.answer()
        settings = get_settings()
        filters = await _resolved_filters(user.id, settings)
        await state.set_state(FilterInputState.waiting_for_value)
        prompt_message = await message.answer(_filter_prompt_text(field_key, filters))
        await state.update_data(
            filter_key=field_key,
            prompt_message_id=prompt_message.message_id,
        )

    @router.message(FilterInputState.waiting_for_value)
    async def save_filter_input(message: Message, state: FSMContext) -> None:
        if not _is_private_chat(message):
            await state.clear()
            await message.answer(_private_only_text())
            return

        state_data = await state.get_data()
        field_key = state_data.get("filter_key")
        if field_key not in FILTER_FIELD_MAP:
            await state.clear()
            await message.answer("Не удалось определить поле настройки. Откройте /settings еще раз.")
            return

        settings = get_settings()
        user = message.from_user
        user_id = user.id if user else 0
        current_filters = await _resolved_filters(user_id, settings)
        try:
            value = validate_filter_value(field_key, message.text or "", current_filters)
        except ValueError as exc:
            await _handle_invalid_filter_input(
                message=message,
                state=state,
                field_key=field_key,
                filters=current_filters,
                error_text=str(exc),
            )
            return

        await storage.aset_user_filter(
            user_id=user_id,
            chat_id=user_id,
            key=field_key,
            value=value,
            username=user.username if user else None,
            first_name=user.first_name if user else None,
        )
        await _delete_prompt_message(message, state_data)
        await state.clear()

        filters, subscriber = await asyncio.gather(
            _resolved_filters(user_id, settings),
            storage.aget_subscriber(user_id),
        )
        is_trial_running = _subscriber_trial_is_running(subscriber)
        is_trial_active = _subscriber_trial_is_active(subscriber)
        lines = [
            f"✅ {filter_field_label(field_key)}: {format_filter_value(field_key, value)}."
        ]
        if is_trial_running:
            lines.append("🔄 Новое значение сохранено. Обновляю поиск в фоне, результат пришлю отдельным сообщением.")
            _start_background_task(
                _run_background_user_recheck(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    prefix_text="✅ Параметр обновлен.",
                ),
                task_name=f"user-recheck:{message.chat.id}",
            )
        elif is_trial_active:
            lines.append("⏸️ Поиск сейчас остановлен. Нажмите «Запустить поиск», чтобы снова его включить.")
        elif subscriber and subscriber.get("started_at"):
            lines.append(_build_trial_ended_text(settings))
        else:
            lines.append("🚀 Нажмите «Запустить поиск», чтобы применить новые параметры.")

        await message.answer(
            "\n\n".join(lines)
            + "\n\n"
            + _build_settings_text(
                settings=settings,
                filters=filters,
                subscriber=subscriber,
                is_trial_running=is_trial_running,
                is_trial_active=is_trial_active,
            ),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                filters=filters,
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
        activation = await storage.aactivate_trial(
            user_id=user.id,
            chat_id=user.id,
            username=user.username,
            first_name=user.first_name,
            duration_hours=settings.trial_duration_hours,
        )
        filters = await _resolved_filters(user.id, settings)
        reply_markup = _build_settings_keyboard(
            InlineKeyboardMarkup,
            InlineKeyboardButton,
            filters=filters,
        )
        just_started = activation["state"] == "started"

        if activation["state"] == "expired":
            await callback.answer()
            await _edit_message_text(
                message,
                text=_build_trial_ended_text(settings),
                reply_markup=reply_markup,
            )
            return

        await callback.answer()
        await _edit_message_text(
            message,
            text=_build_start_work_message_text(
                settings=settings,
                filters=filters,
                subscriber=activation["subscriber"],
                just_started=just_started,
                result_text="🔄 Запускаю проверку в фоне...",
            ),
            reply_markup=reply_markup,
        )

        _start_background_task(
            _run_background_start_check(
                bot=message.bot,
                chat_id=message.chat.id,
                message_id=message.message_id,
                settings=settings,
                filters=filters,
                subscriber=activation["subscriber"],
                just_started=just_started,
                reply_markup=reply_markup,
            ),
            task_name=f"start-check:{message.chat.id}:{message.message_id}",
        )

    @router.callback_query(F.data.startswith("offer_batch:"))
    async def offer_batch_page(callback: CallbackQuery) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None:
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Просмотр доступен только в личных сообщениях.", show_alert=True)
            return

        from app.notifier import parse_offer_batch_callback_data, show_offer_batch_page

        parsed = parse_offer_batch_callback_data(callback.data or "")
        if parsed is None:
            await callback.answer("Не удалось открыть страницу списка.", show_alert=True)
            return

        session_id, page_index = parsed
        try:
            current_page, total_pages = await show_offer_batch_page(
                session_id,
                page_index,
                chat_id=message.chat.id,
                bot=message.bot,
            )
        except KeyError:
            await callback.answer("Этот список больше недоступен. Запустите поиск снова.", show_alert=True)
            return

        await callback.answer(f"Страница {current_page} из {total_pages}")

    @router.callback_query(F.data == "reset_filters")
    async def reset_filters(callback: CallbackQuery, state: FSMContext) -> None:
        message = callback.message
        user = callback.from_user
        if message is None or user is None:
            await callback.answer()
            return
        if not _is_private_chat(message):
            await callback.answer("Настройка доступна только в личных сообщениях.", show_alert=True)
            return

        state_data = await state.get_data()
        await _delete_prompt_message(message, state_data)
        await state.clear()

        settings = get_settings()
        await storage.aset_user_filters(
            user_id=user.id,
            chat_id=user.id,
            filters=empty_user_filters(),
            username=user.username,
            first_name=user.first_name,
        )
        filters, subscriber = await asyncio.gather(
            _resolved_filters(user.id, settings),
            storage.aget_subscriber(user.id),
        )
        is_trial_running = _subscriber_trial_is_running(subscriber)
        is_trial_active = _subscriber_trial_is_active(subscriber)
        lines = ["🧹 Все фильтры сброшены."]

        if is_trial_running:
            lines.append("🔄 Фильтры сброшены. Обновляю поиск в фоне, результат пришлю отдельным сообщением.")
            _start_background_task(
                _run_background_user_recheck(
                    bot=message.bot,
                    chat_id=message.chat.id,
                    prefix_text="🧹 Фильтры сброшены.",
                ),
                task_name=f"user-recheck:{message.chat.id}",
            )
        elif is_trial_active:
            lines.append("⏸️ Поиск сейчас остановлен. Нажмите «Запустить поиск», чтобы снова его включить.")
        elif subscriber and subscriber.get("started_at"):
            lines.append(_build_trial_ended_text(settings))
        else:
            lines.append("🚀 Нажмите «Запустить поиск», чтобы запустить поиск с чистыми параметрами.")

        await message.answer(
            "\n\n".join(lines)
            + "\n\n"
            + _build_settings_text(
                settings=settings,
                filters=filters,
                subscriber=subscriber,
                is_trial_running=is_trial_running,
                is_trial_active=is_trial_active,
            ),
            reply_markup=_build_settings_keyboard(
                InlineKeyboardMarkup,
                InlineKeyboardButton,
                filters=filters,
            ),
        )
        await callback.answer()

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        settings = get_settings()
        user_id = message.from_user.id if message.from_user else 0
        admin_access = is_authorized(user_id=user_id, chat_id=message.chat.id, settings=settings)
        subscriber, filters, last_check = await asyncio.gather(
            storage.aget_subscriber(user_id),
            _resolved_filters(user_id, settings),
            storage.aget_last_check(),
        )
        last_check_time = _format_datetime(last_check["checked_at"] if last_check else None)

        if admin_access:
            default_threshold = await _default_threshold(settings)
            await message.answer(
                "🛠 Бот работает\n"
                f"🎯 Порог по умолчанию: {_format_number(default_threshold)}\n"
                f"⏱ Интервал проверки: {settings.check_interval_seconds} сек.\n"
                f"🕒 Последняя проверка: {last_check_time}"
            )
            return

        if not _is_private_chat(message):
            await message.answer(_private_only_text())
            return

        is_trial_running = _subscriber_trial_is_running(subscriber)
        is_trial_active = _subscriber_trial_is_active(subscriber)

        if is_trial_running:
            await message.answer(
                "🟢 Поиск активен\n"
                f"🎯 Скор от: {_format_filter_brief(filters, 'borrower_score_min')}\n"
                f"💰 Сумма: {_format_filter_range(filters, 'amount_min', 'amount_max')}\n"
                f"📆 Срок: {_format_filter_range(filters, 'term_min', 'term_max')}\n"
                f"⏱ Проверка каждые {settings.check_interval_seconds} сек.\n"
                f"⌛ Доступ до: {_format_datetime(subscriber.get('expires_at') if subscriber else None)}\n"
                f"🕒 Последняя проверка: {last_check_time}"
            )
            return

        if subscriber and subscriber.get("started_at") and is_trial_active:
            await message.answer(
                "⏸️ Поиск остановлен\n"
                f"🎯 Скор от: {_format_filter_brief(filters, 'borrower_score_min')}\n"
                f"💰 Сумма: {_format_filter_range(filters, 'amount_min', 'amount_max')}\n"
                f"📆 Срок: {_format_filter_range(filters, 'term_min', 'term_max')}\n"
                "▶️ Чтобы продолжить, нажмите «Запустить поиск» в /settings.\n"
                f"⌛ Доступ до: {_format_datetime(subscriber.get('expires_at'))}\n"
                f"🕒 Последняя проверка: {last_check_time}"
            )
            return

        if subscriber and subscriber.get("started_at"):
            await message.answer(_build_trial_ended_text(settings))
            return

        await message.answer(
            "🚀 Поиск еще не запущен.\n"
            "Откройте /settings, настройте фильтры и нажмите «Запустить поиск»."
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
            await message.answer("⚠️ Значение порога должно быть числом.")
            return

        if value < 0 or value > 100:
            await message.answer("⚠️ Порог должен быть в диапазоне 0 <= threshold <= 100.")
            return

        await storage.aset_threshold(value)
        await message.answer(
            f"🎯 Порог по умолчанию изменен на {_format_number(value)}.\n"
            "🔄 Запускаю повторную проверку для активных пользователей."
        )

        from app.monitor import check_after_threshold_change

        try:
            offers_count, notified_count = await check_after_threshold_change(bot=message.bot)
        except Exception as exc:
            await message.answer(f"⚠️ Порог сохранен, но проверка завершилась ошибкой: {exc}")
            return

        await message.answer(
            "✅ Проверка по новому порогу выполнена.\n"
            f"📊 Найдено предложений: {offers_count}\n"
            f"📬 Уведомлений отправлено: {notified_count}"
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
            await message.answer(f"⚠️ Проверка завершилась ошибкой: {exc}")
            return

        await message.answer(
            "✅ Проверка выполнена.\n"
            f"📊 Найдено предложений: {offers_count}\n"
            f"📬 Уведомлений отправлено: {notified_count}"
        )

    @router.message(Command("last"))
    async def last(message: Message) -> None:
        if not await _ensure_user_access(message):
            return

        offers = await storage.aget_recent_offers(limit=5)
        if not offers:
            await message.answer("📭 Сохраненных предложений пока нет.")
            return

        lines = ["🧾 Последние сохраненные предложения:"]
        for offer in offers:
            lines.append(
                "\n"
                f"ID: {offer['offer_id']}\n"
                f"🎯 Скор балл: {_format_number(offer['score'])}\n"
                f"🕒 Первое появление: {offer['first_seen_at']}\n"
                f"📬 Уведомление: {offer['notified_at'] or '-'}\n"
                f"🔗 Ссылка: {offer['url'] or '-'}"
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
            "📚 Команды:",
            "/start - главное меню и остановка поиска",
            "/settings - настроить фильтры и запустить поиск",
            "/status - статус поиска и срока доступа",
            "/last - последние 5 найденных предложений",
            "/help - список команд",
        ]
        if _is_admin_message(message):
            lines.extend(
                [
                    "/admin - админка: статистика, пользователи, рассылки",
                    "/threshold 70 - изменить порог по умолчанию",
                    "/check - запустить проверку вручную",
                    "/chat_id - показать ID текущего чата",
                ]
            )
        else:
            lines.append(
                f"🎁 После активации бесплатный доступ работает {settings.trial_duration_hours} часа(ов)."
            )
        await message.answer("\n".join(lines))

    @router.message(F.text)
    async def plain_text_fallback(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return
        if not _is_private_chat(message):
            return

        current_state = await state.get_state()
        if current_state:
            return

        await message.answer(
            "✍️ Сначала нажмите нужную кнопку параметра, потом отправьте значение.\n"
            "Если хотите открыть настройки заново, используйте /settings."
        )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def register_bot_commands(bot: Any) -> None:
    from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    user_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="settings", description="Фильтры и запуск"),
        BotCommand(command="status", description="Статус доступа"),
        BotCommand(command="last", description="Последние предложения"),
        BotCommand(command="help", description="Список команд"),
    ]
    admin_commands = [
        *user_commands,
        BotCommand(command="admin", description="Админка"),
        BotCommand(command="threshold", description="Изменить порог"),
        BotCommand(command="check", description="Проверить сейчас"),
        BotCommand(command="chat_id", description="Показать chat ID"),
    ]

    async def set_commands_with_retry(commands: list[Any], *, scope: Any, scope_name: str) -> bool:
        retry_delay = COMMAND_SYNC_RETRY_DELAY_SECONDS
        for attempt in range(1, COMMAND_SYNC_RETRIES + 1):
            try:
                await bot.set_my_commands(commands, scope=scope)
                return True
            except TelegramRetryAfter as exc:
                logger.warning(
                    "telegram flood control while registering bot commands scope=%s retry_after=%s",
                    scope_name,
                    exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                if attempt >= COMMAND_SYNC_RETRIES:
                    logger.warning(
                        "failed to register bot commands after %s attempts scope=%s error=%s",
                        attempt,
                        scope_name,
                        exc,
                    )
                    return False
                logger.warning(
                    "telegram network error while registering bot commands attempt=%s/%s scope=%s retry_in=%ss error=%s",
                    attempt,
                    COMMAND_SYNC_RETRIES,
                    scope_name,
                    retry_delay,
                    exc,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
            except Exception:
                logger.exception("unexpected error while registering bot commands scope=%s", scope_name)
                return False
        return False

    await set_commands_with_retry(
        user_commands,
        scope=BotCommandScopeDefault(),
        scope_name="default",
    )

    for admin_user_id in get_settings().telegram_allowed_user_ids:
        await set_commands_with_retry(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=int(admin_user_id)),
            scope_name=f"chat:{int(admin_user_id)}",
        )


def _build_admin_keyboard(markup_cls: Any, button_cls: Any) -> Any:
    return markup_cls(
        inline_keyboard=[
            [
                button_cls(text="Статистика", callback_data=f"{ADMIN_CALLBACK_PREFIX}stats"),
                button_cls(text="Пользователи", callback_data=f"{ADMIN_CALLBACK_PREFIX}users"),
            ],
            [
                button_cls(
                    text="Рассылка всем",
                    callback_data=f"{ADMIN_CALLBACK_PREFIX}broadcast_all",
                )
            ],
            [
                button_cls(
                    text="Рассылка по ID",
                    callback_data=f"{ADMIN_CALLBACK_PREFIX}broadcast_user",
                )
            ],
        ]
    )


def _build_admin_back_keyboard(markup_cls: Any, button_cls: Any) -> Any:
    return markup_cls(
        inline_keyboard=[
            [button_cls(text="Назад", callback_data=f"{ADMIN_CALLBACK_PREFIX}menu")]
        ]
    )


def _build_admin_cancel_keyboard(markup_cls: Any, button_cls: Any) -> Any:
    return markup_cls(
        inline_keyboard=[
            [button_cls(text="Отмена", callback_data=f"{ADMIN_CALLBACK_PREFIX}cancel")]
        ]
    )


async def _build_admin_users_page(
    markup_cls: Any,
    button_cls: Any,
    *,
    page_index: int,
) -> tuple[int, int, str, Any]:
    users, total_count = await storage.aget_subscribers_page(
        limit=ADMIN_USERS_PAGE_SIZE,
        offset=max(0, page_index) * ADMIN_USERS_PAGE_SIZE,
    )
    total_pages = max(1, (total_count + ADMIN_USERS_PAGE_SIZE - 1) // ADMIN_USERS_PAGE_SIZE)
    safe_page_index = min(max(0, page_index), total_pages - 1)
    if safe_page_index != max(0, page_index):
        users, total_count = await storage.aget_subscribers_page(
            limit=ADMIN_USERS_PAGE_SIZE,
            offset=safe_page_index * ADMIN_USERS_PAGE_SIZE,
        )

    settings = get_settings()
    default_threshold = await _default_threshold(settings)
    raw_filters_list = await asyncio.gather(
        *(storage.aget_user_filters(int(user["user_id"])) for user in users)
    ) if users else []
    filters_by_user = {
        int(user["user_id"]): resolved_user_filters(raw_filters, default_threshold)
        for user, raw_filters in zip(users, raw_filters_list, strict=False)
    }
    text = _build_admin_users_text(
        users,
        filters_by_user=filters_by_user,
        total_count=total_count,
        page_index=safe_page_index,
    )
    reply_markup = _build_admin_users_keyboard(
        markup_cls,
        button_cls,
        page_index=safe_page_index,
        total_pages=total_pages,
    )
    return safe_page_index + 1, total_pages, text, reply_markup


def _build_admin_users_keyboard(
    markup_cls: Any,
    button_cls: Any,
    *,
    page_index: int,
    total_pages: int,
) -> Any:
    rows: list[list[Any]] = []
    navigation: list[Any] = []
    if page_index > 0:
        navigation.append(
            button_cls(
                text="Назад 10",
                callback_data=f"{ADMIN_CALLBACK_PREFIX}users:{page_index - 1}",
            )
        )
    navigation.append(
        button_cls(
            text=f"{page_index + 1}/{total_pages}",
            callback_data=ADMIN_NOOP_CALLBACK,
        )
    )
    if page_index < total_pages - 1:
        navigation.append(
            button_cls(
                text="Вперед 10",
                callback_data=f"{ADMIN_CALLBACK_PREFIX}users:{page_index + 1}",
            )
        )
    rows.append(navigation)
    rows.append([button_cls(text="Назад", callback_data=f"{ADMIN_CALLBACK_PREFIX}menu")])
    return markup_cls(inline_keyboard=rows)


async def _build_admin_home_text() -> str:
    stats, last_check = await asyncio.gather(
        storage.aget_user_stats(),
        storage.aget_last_check(),
    )
    last_check_time = last_check["checked_at"] if last_check else None
    return (
        "🛠 Админка FinKit Score Bot\n\n"
        f"👥 Пользователей в базе: {_safe_int(stats.get('total_users'))}\n"
        f"🟢 Поиск активен: {_safe_int(stats.get('running_users'))}\n"
        f"⏸️ На паузе: {_safe_int(stats.get('paused_users'))}\n"
        f"⌛ Период завершен: {_safe_int(stats.get('expired_users'))}\n"
        f"🕒 Последняя проверка: {_format_datetime(last_check_time)}\n\n"
        "Выберите действие ниже."
    )


def _build_admin_stats_text(stats: dict[str, Any]) -> str:
    return (
        "📊 Статистика пользователей\n\n"
        f"🗓 За 24 часа: {_safe_int(stats.get('registrations_day'))}\n"
        f"📅 За 7 дней: {_safe_int(stats.get('registrations_week'))}\n"
        f"🗓 За 30 дней: {_safe_int(stats.get('registrations_month'))}\n"
        f"👥 За все время: {_safe_int(stats.get('total_users'))}\n\n"
        f"⚙️ С фильтрами: {_safe_int(stats.get('configured_users'))}\n"
        f"🚀 Запускали поиск: {_safe_int(stats.get('activated_users'))}\n"
        f"🟢 Активных сейчас: {_safe_int(stats.get('active_users'))}\n"
        f"🔔 Поиск включен: {_safe_int(stats.get('running_users'))}\n"
        f"⏸️ Поиск остановлен: {_safe_int(stats.get('paused_users'))}\n"
        f"⌛ Период завершен: {_safe_int(stats.get('expired_users'))}\n\n"
        f"🆕 Последняя регистрация: {_format_datetime(stats.get('last_registration_at'))}\n"
        f"🕒 Последний запуск: {_format_datetime(stats.get('last_activation_at'))}"
    )


def _build_admin_users_text(
    users: list[dict[str, Any]],
    *,
    filters_by_user: dict[int, dict[str, Any]],
    total_count: int,
    page_index: int,
) -> str:
    total_pages = max(1, (total_count + ADMIN_USERS_PAGE_SIZE - 1) // ADMIN_USERS_PAGE_SIZE)
    if not users:
        return "📭 Пользователей в базе пока нет."

    lines = [
        f"👥 Пользователи в базе: {total_count}",
        f"📄 Страница {page_index + 1} из {total_pages}",
        "",
    ]

    start_number = page_index * ADMIN_USERS_PAGE_SIZE + 1
    for number, user in enumerate(users, start=start_number):
        user_id = int(user["user_id"])
        username = user.get("username")
        first_name = user.get("first_name") or "-"
        filters = filters_by_user.get(user_id, {})
        lines.extend(
            [
                f"{number}. 👤 ID {user_id} | {_format_username(username)} | {first_name}",
                (
                    f"🗂 В базе с: {_format_datetime(user.get('created_at'))}"
                    f" | 🚀 Старт: {_format_datetime(user.get('started_at'))}"
                ),
                (
                    f"📌 Статус: {_admin_user_status_text(user)}"
                    f" | ⌛ До: {_format_datetime(user.get('expires_at'))}"
                ),
                (
                    "⚙️ Фильтры: "
                    f"{_compact_filters_text(filters)}"
                    f" | 🔚 Финал: {_format_datetime(user.get('trial_ended_notified_at'))}"
                ),
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def _build_admin_broadcast_all_prompt(recipients_count: int) -> str:
    return (
        "📢 Рассылка всем пользователям\n\n"
        f"👥 Получателей в базе: {recipients_count}\n\n"
        "Отправьте одним сообщением текст, фото, видео или другой контент для рассылки."
    )


def _build_admin_target_prompt(target_user_id: int, subscriber: dict[str, Any] | None) -> str:
    if subscriber is None:
        return (
            f"⚠️ Пользователь {target_user_id} в базе не найден.\n"
            "Если он уже открывал чат с ботом, Telegram все равно может принять отправку.\n\n"
            "Теперь отправьте сообщение для пересылки."
        )

    return (
        f"👤 Получатель: {target_user_id}\n"
        f"🔗 Username: {_format_username(subscriber.get('username'))}\n"
        f"🪪 Имя: {subscriber.get('first_name') or '-'}\n"
        f"📌 Статус: {_admin_user_status_text(subscriber)}\n\n"
        "Теперь отправьте сообщение для пересылки."
    )


def _build_admin_broadcast_result_text(
    *,
    total_recipients: int,
    sent_count: int,
    failed_ids: list[int],
) -> str:
    lines = [
        "✅ Рассылка завершена.",
        f"👥 Получателей: {total_recipients}",
        f"📬 Успешно: {sent_count}",
        f"⚠️ Ошибок: {len(failed_ids)}",
    ]
    if failed_ids:
        preview = ", ".join(str(user_id) for user_id in failed_ids[:20])
        suffix = " ..." if len(failed_ids) > 20 else ""
        lines.append(f"Не доставлено ID: {preview}{suffix}")
    return "\n".join(lines)


def _build_admin_single_broadcast_result_text(
    *,
    target_user_id: int,
    sent_count: int,
    failed_ids: list[int],
) -> str:
    if sent_count > 0:
        return f"✅ Сообщение отправлено пользователю {target_user_id}."
    return (
        f"⚠️ Не удалось отправить сообщение пользователю {target_user_id}.\n"
        f"Ошибка по ID: {', '.join(str(user_id) for user_id in failed_ids) or '-'}"
    )


def _compact_filters_text(filters: dict[str, Any]) -> str:
    return "; ".join(
        [
            f"скор {_format_filter_range(filters, 'borrower_score_min', 'borrower_score_max')}",
            f"сумма {_format_filter_range(filters, 'amount_min', 'amount_max')}",
            f"срок {_format_filter_range(filters, 'term_min', 'term_max')}",
            f"ставка {_format_filter_range(filters, 'interest_rate_min', 'interest_rate_max')}",
            f"рейтинг {_format_filter_range(filters, 'borrower_rating_min', 'borrower_rating_max')}",
            f"инвест {_format_filter_range(filters, 'invest_min', 'invest_max')}",
            f"доход {_format_filter_brief(filters, 'borrower_income_confirmed')}",
            (
                "исп.пр-ва "
                f"{_format_filter_brief(filters, 'borrower_enforcement_up_to_1_month_absent')}"
            ),
            f"возраст {_format_filter_brief(filters, 'borrower_age_group')}",
        ]
    )


def _format_username(username: object) -> str:
    value = str(username).strip() if username is not None else ""
    return f"@{value}" if value else "-"


def _safe_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _admin_user_status_text(user: dict[str, Any]) -> str:
    if not user.get("started_at"):
        return "не запускал"
    if _subscriber_trial_is_active(user):
        return "активен" if _row_search_enabled(user) else "на паузе"
    return "период завершен"


def _row_trial_is_active(user: dict[str, Any]) -> bool:
    expires_at = user.get("expires_at")
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(str(expires_at)) > datetime.now(timezone.utc)
    except ValueError:
        return False


def _row_search_enabled(user: dict[str, Any]) -> bool:
    value = user.get("search_enabled")
    if value is None:
        return True
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _row_is_private_subscriber(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    try:
        return int(user.get("chat_id")) == int(user.get("user_id"))
    except (TypeError, ValueError):
        return False


def _subscriber_trial_is_active(user: dict[str, Any] | None) -> bool:
    return bool(user) and _row_is_private_subscriber(user) and _row_trial_is_active(user)


def _subscriber_trial_is_running(user: dict[str, Any] | None) -> bool:
    return _subscriber_trial_is_active(user) and _row_search_enabled(user)


async def _handle_admin_escape_command(
    message: Any,
    state: Any,
    markup_cls: Any,
    button_cls: Any,
) -> bool:
    text = (getattr(message, "text", None) or "").strip()
    user = getattr(message, "from_user", None)
    if text != "/admin" or user is None or not is_explicitly_allowed_user(user.id):
        return False
    await state.clear()
    await message.answer(
        await _build_admin_home_text(),
        reply_markup=_build_admin_keyboard(markup_cls, button_cls),
    )
    return True


async def _broadcast_message_copy(message: Any, user_ids: list[int]) -> tuple[int, list[int]]:
    from app.notifier import copy_message_to_chat

    source_chat = getattr(message, "chat", None)
    source_chat_id = getattr(source_chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if not isinstance(source_chat_id, int) or not isinstance(message_id, int):
        return 0, list(user_ids)

    sent_count = 0
    failed_ids: list[int] = []
    for user_id in user_ids:
        try:
            await copy_message_to_chat(
                source_chat_id=source_chat_id,
                message_id=message_id,
                chat_id=int(user_id),
                bot=message.bot,
            )
        except Exception:
            failed_ids.append(int(user_id))
        else:
            sent_count += 1
    return sent_count, failed_ids


def _start_background_task(task: Any, *, task_name: str) -> None:
    created_task = asyncio.create_task(task, name=task_name)

    def on_done(done_task: asyncio.Task[Any]) -> None:
        try:
            done_task.result()
        except Exception:
            logger.exception("background task failed task_name=%s", task_name)

    created_task.add_done_callback(on_done)


async def _run_background_user_recheck(
    *,
    bot: Any,
    chat_id: int,
    prefix_text: str,
) -> None:
    from app.monitor import check_once

    try:
        offers_count, notified_count = await check_once(bot=bot)
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{prefix_text}\n\n⚠️ Фоновая проверка завершилась ошибкой: {exc}",
        )
        return

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"{prefix_text}\n\n"
            f"✅ Фоновая проверка завершена.\n"
            f"📊 Найдено предложений: {offers_count}\n"
            f"📬 Уведомлений отправлено: {notified_count}"
        ),
    )


async def _run_background_start_check(
    *,
    bot: Any,
    chat_id: int,
    message_id: int,
    settings: Settings,
    filters: dict[str, Any],
    subscriber: dict[str, Any] | None,
    just_started: bool,
    reply_markup: Any,
) -> None:
    from app.monitor import check_after_threshold_change, check_once

    try:
        if just_started:
            offers_count, notified_count = await check_once(bot=bot)
        else:
            offers_count, notified_count = await check_after_threshold_change(bot=bot)
    except Exception as exc:
        text = _build_start_work_message_text(
            settings=settings,
            filters=filters,
            subscriber=subscriber,
            just_started=just_started,
            result_text=f"⚠️ Поиск запущен, но проверка завершилась ошибкой: {exc}",
        )
    else:
        text = _build_start_work_message_text(
            settings=settings,
            filters=filters,
            subscriber=subscriber,
            just_started=just_started,
            result_text=_build_start_check_result_text(
                just_started=just_started,
                offers_count=offers_count,
                notified_count=notified_count,
            ),
        )

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.exception("failed to update start check message chat_id=%s message_id=%s", chat_id, message_id)


def _build_settings_keyboard(markup_cls: Any, button_cls: Any, *, filters: dict[str, Any]) -> Any:
    rows: list[list[Any]] = []
    for row_keys in FILTER_BUTTON_ROWS:
        rows.append(
            [
                button_cls(
                    text=filter_button_text(field_key, filters.get(field_key)),
                    callback_data=f"edit_filter:{field_key}",
                )
                for field_key in row_keys
            ]
        )
    rows.append([button_cls(text="Сбросить все фильтры", callback_data="reset_filters")])
    rows.append([button_cls(text="Запустить поиск", callback_data="start_work")])
    return markup_cls(inline_keyboard=rows)


def _build_welcome_text(
    settings: Settings,
    filters: dict[str, Any],
    *,
    search_was_stopped: bool,
) -> str:
    threshold = filters.get("borrower_score_min")
    text = (
        "👋 FinKit Score Bot помогает находить новые заявки FinKit и присылает их в ЛС.\n\n"
        f"🎯 Стартовый фильтр: скор {_comparator(settings)} {_format_number(threshold)}.\n"
        f"🎁 Бесплатный доступ: {settings.trial_duration_hours} часа(ов).\n\n"
        "⚙️ Выберите нужные параметры ниже и запустите поиск."
    )
    if search_was_stopped:
        text = (
            "⏸️ Поиск был остановлен для этого пользователя.\n"
            "▶️ Чтобы включить его снова, нажмите «Запустить поиск».\n\n"
            + text
        )
    return text


def _build_settings_text(
    *,
    settings: Settings,
    filters: dict[str, Any],
    subscriber: dict[str, Any] | None,
    is_trial_running: bool,
    is_trial_active: bool,
) -> str:
    if subscriber and subscriber.get("started_at") and is_trial_running:
        status_text = (
            "🟢 Поиск активен.\n"
            f"⌛ Доступ до: {_format_datetime(subscriber.get('expires_at'))}\n"
        )
    elif subscriber and subscriber.get("started_at") and is_trial_active:
        status_text = (
            "⏸️ Поиск остановлен.\n"
            f"⌛ Доступ до: {_format_datetime(subscriber.get('expires_at'))}\n"
            "▶️ Нажмите «Запустить поиск», чтобы снова его включить.\n"
        )
    elif subscriber and subscriber.get("started_at"):
        status_text = "⌛ Бесплатный период уже закончился.\n"
    else:
        status_text = "🚀 Поиск еще не запущен.\n"

    return (
        "⚙️ Настройки поиска\n\n"
        f"{status_text}"
        "📌 Параметры заявки\n"
        f"🎯 Скор: {_format_filter_range(filters, 'borrower_score_min', 'borrower_score_max')}\n"
        f"💰 Сумма: {_format_filter_range(filters, 'amount_min', 'amount_max')}\n"
        f"📆 Срок: {_format_filter_range(filters, 'term_min', 'term_max')}\n"
        f"📈 Ставка: {_format_filter_range(filters, 'interest_rate_min', 'interest_rate_max')}\n"
        f"🏷️ Рейтинг: {_format_filter_range(filters, 'borrower_rating_min', 'borrower_rating_max')}\n"
        f"💼 Инвест: {_format_filter_range(filters, 'invest_min', 'invest_max')}\n\n"
        "👤 Профиль заемщика\n"
        f"💳 Доход подтвержден: {_format_filter_brief(filters, 'borrower_income_confirmed')}\n"
        f"📄 Исп. пр-ва: {_format_filter_brief(filters, 'borrower_enforcement_up_to_1_month_absent')}\n"
        f"🪪 Возраст: {_format_filter_brief(filters, 'borrower_age_group')}\n\n"
        "Ниже можно менять параметры в любом порядке."
    )


def _build_active_trial_text(
    *,
    settings: Settings,
    filters: dict[str, Any],
    subscriber: dict[str, Any] | None,
    just_started: bool,
) -> str:
    expires_at = subscriber.get("expires_at") if subscriber else None
    intro = "✅ Поиск запущен." if just_started else "🟢 Поиск уже активен."
    return (
        f"{intro}\n\n"
        f"⏱ Проверка каждые {settings.check_interval_seconds} сек.\n"
        f"🎯 Скор: {_format_filter_range(filters, 'borrower_score_min', 'borrower_score_max')}\n"
        f"💰 Сумма: {_format_filter_range(filters, 'amount_min', 'amount_max')}\n"
        f"📆 Срок: {_format_filter_range(filters, 'term_min', 'term_max')}\n"
        f"📈 Ставка: {_format_filter_range(filters, 'interest_rate_min', 'interest_rate_max')}\n"
        "📬 Как только появится подходящее предложение, я отправлю его в личные сообщения.\n"
        f"⌛ Доступ действует до: {_format_datetime(expires_at)}"
    )


def _filter_prompt_text(field_key: str, filters: dict[str, Any], error_text: str | None = None) -> str:
    base_text = f"{filter_field_label(field_key)}\n\n{filter_prompt(field_key, filters)}"
    if not error_text:
        return base_text
    return f"{filter_field_label(field_key)}\n\nОшибка: {error_text}\n\n{filter_prompt(field_key, filters)}"


async def _handle_invalid_filter_input(
    *,
    message: Any,
    state: Any,
    field_key: str,
    filters: dict[str, Any],
    error_text: str,
) -> None:
    state_data = await state.get_data()
    prompt_message_id = state_data.get("prompt_message_id")

    try:
        await message.delete()
    except Exception:
        pass

    text = _filter_prompt_text(field_key, filters, error_text)
    if prompt_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text=text,
            )
            return
        except Exception:
            pass

    prompt_message = await message.answer(text)
    await state.update_data(prompt_message_id=prompt_message.message_id)


async def _delete_prompt_message(message: Any, state_data: dict[str, Any]) -> None:
    prompt_message_id = state_data.get("prompt_message_id")
    if not prompt_message_id:
        return
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
    except Exception:
        pass


def _build_trial_ended_text(settings: Settings) -> str:
    return (
        "⌛ Бесплатный период закончился.\n\n"
        f"💬 Чтобы продолжить работу, напишите {settings.trial_manager_contact}."
    )


def _build_start_check_result_text(
    *,
    just_started: bool,
    offers_count: int,
    notified_count: int,
) -> str:
    if notified_count > 0:
        prefix = "✅ Первая проверка выполнена." if just_started else "✅ Проверка выполнена."
        if notified_count > 10:
            return (
                f"{prefix}\n"
                f"📊 Найдено предложений: {offers_count}\n"
                f"📬 Подходящих предложений: {notified_count}\n"
                "📄 Показаны первые 10. Остальные можно открыть кнопками ниже."
            )
        return (
            f"{prefix}\n"
            f"📊 Найдено предложений: {offers_count}\n"
            f"📬 Отправлено уведомлений: {notified_count}"
        )

    if offers_count <= 0:
        return "ℹ️ Проверка выполнена, но площадка сейчас не вернула доступных предложений."

    return (
        "✅ Проверка выполнена.\n"
        f"📊 Найдено предложений: {offers_count}\n"
        "🔎 По вашим фильтрам подходящих предложений сейчас нет."
    )


def _build_start_work_message_text(
    *,
    settings: Settings,
    filters: dict[str, Any],
    subscriber: dict[str, Any] | None,
    just_started: bool,
    result_text: str | None = None,
) -> str:
    parts = [
        _build_active_trial_text(
            settings=settings,
            filters=filters,
            subscriber=subscriber,
            just_started=just_started,
        )
    ]
    if result_text:
        parts.append(result_text)
    return "\n\n".join(parts)


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


def _format_filter_brief(filters: dict[str, Any], key: str) -> str:
    return format_filter_value(key, filters.get(key))


def _format_filter_range(filters: dict[str, Any], min_key: str, max_key: str) -> str:
    return f"{format_filter_value(min_key, filters.get(min_key))} .. {format_filter_value(max_key, filters.get(max_key))}"


def _comparator(settings: Settings) -> str:
    return ">=" if settings.score_compare_mode == "gte" else ">"


async def _default_threshold(settings: Settings) -> float:
    return await storage.aget_threshold(settings.default_score_threshold)


async def _resolved_filters(user_id: int, settings: Settings) -> dict[str, Any]:
    raw_filters, default_threshold = await asyncio.gather(
        storage.aget_user_filters(user_id),
        _default_threshold(settings),
    )
    return resolved_user_filters(raw_filters, default_threshold)


async def _edit_message_text(message: Any, *, text: str, reply_markup: Any | None = None) -> None:
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


async def _ensure_user_access(message: Any) -> bool:
    if _is_admin_message(message):
        return True

    if not _is_private_chat(message):
        await message.answer(_private_only_text())
        return False

    user = getattr(message, "from_user", None)
    user_id = user.id if user else 0

    subscriber = await storage.aget_subscriber(user_id)
    if _subscriber_trial_is_active(subscriber):
        return True

    if subscriber and subscriber.get("started_at"):
        await message.answer(_build_trial_ended_text(get_settings()))
        return False

    await message.answer("🚀 Сначала откройте /settings, задайте фильтры и нажмите «Запустить поиск».")
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
    return "🔒 Эта функция работает только в личных сообщениях. Напишите боту в ЛС и используйте /start или /settings."
