import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.handlers import router
from src.config import settings
from src.db import QueueDB
from src.schedule import ScheduleClient
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


async def main() -> None:
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = QueueDB(settings.database_path)
    await db.initialize()
    schedule = ScheduleClient(settings.schedule_api_base, settings.schedule_proxy)

    dp = Dispatcher(storage=MemoryStorage(), db=db, schedule=schedule)
    dp.include_router(router)

    refresh_task = asyncio.create_task(
        refresh_loop(schedule, db, settings.group_name, settings.schedule_api_base)
    )
    try:
        await wait_for_telegram(bot)
        # Sequential handling keeps the order of updates that piled up while the bot was offline,
        # so the queue order matches the order in which people pressed "Записаться".
        await dp.start_polling(bot, handle_as_tasks=False)
    finally:
        refresh_task.cancel()
        await schedule.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
