import asyncio
import logging

from typing import Any, Awaitable, Callable, Dict

import asyncpg

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject, User

from src.bot.formatting import clean_text
from src.bot.handlers import router
from src.config import settings
from src.db import QueueDB
from src.schedule import FileSchedule
from src.sync import refresh_loop

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class AutoRegisterMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user") or getattr(event, "from_user", None)
        db: QueueDB | None = data.get("db")
        if user and not user.is_bot and db:
            name = clean_text(user.full_name)[:60] or "Пользователь"
            await db.ensure_profile(user.id, name)
        return await handler(event, data)


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
    auto_reg = AutoRegisterMiddleware()
    dp.message.outer_middleware(auto_reg)
    dp.callback_query.outer_middleware(auto_reg)
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
