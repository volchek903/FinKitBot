import asyncio
import logging

from app import storage
from app.bot import create_dispatcher, register_bot_commands
from app.config import get_settings
from app.logging_config import setup_logging
from app.notifier import notify_admin_error

logger = logging.getLogger(__name__)


async def monitoring_loop(telegram_bot: object) -> None:
    settings = get_settings()
    backoff_seconds = 60

    while True:
        try:
            from app.monitor import check_once

            await check_once(bot=telegram_bot)
            backoff_seconds = 60
            await asyncio.sleep(settings.check_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("monitoring loop failed")
            await notify_admin_error("Ошибка мониторинга", str(exc))
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(300, backoff_seconds * 2)


async def main() -> None:
    setup_logging()
    settings = get_settings()
    await storage.ainit_db()

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    from aiogram import Bot as TelegramBot

    telegram_bot = TelegramBot(token=settings.telegram_bot_token)
    dispatcher = create_dispatcher()
    monitor_task = asyncio.create_task(monitoring_loop(telegram_bot))

    try:
        await register_bot_commands(telegram_bot)
        await dispatcher.start_polling(telegram_bot)
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await telegram_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
