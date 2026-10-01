import asyncio
import logging

import asyncpg

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.handlers import router
from src.config import settings
from src.db import QueueDB
from src.schedule import FileSchedule
from src.sync import refresh_loop

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def wait_for_telegram(bot: Bot) -> None:
    delay = 2
    while True:
        try:
            await bot.get_me()
            return
        except TelegramNetworkError as exc:
            log.warning("Telegram API is unreachable (%s); retrying in %s seconds.", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def connect_db(db: QueueDB, attempts: int = 10) -> None:
    """Postgres may still be booting when the bot starts (e.g. right after a Railway deploy)."""
    for attempt in range(1, attempts + 1):
        try:
            await db.initialize()
            return
        except (OSError, asyncpg.PostgresError) as exc:
            if attempt == attempts:
                raise
            log.warning("Database is not ready (%s: %s); retry %s/%s in 3 s.", type(exc).__name__, exc, attempt, attempts)
            await asyncio.sleep(3)


async def main() -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = QueueDB(settings.database_url)
    await connect_db(db)
    schedule = FileSchedule(settings.schedule_file, settings.group_name)

    dp = Dispatcher(storage=MemoryStorage(), db=db)
    dp.include_router(router)

    refresh_task = asyncio.create_task(refresh_loop(schedule, db))
    try:
        await wait_for_telegram(bot)
        # Sequential handling keeps the order of updates that piled up while the bot was offline,
        # so the queue order matches the order in which people pressed "Записаться".
        await dp.start_polling(bot, handle_as_tasks=False)
    finally:
        refresh_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
