import asyncio
import logging

from src.db import QueueDB
from src.schedule import FileSchedule

# The bundled timetable does not change, but the 14-day window moves every day.
REFRESH_INTERVAL = 6 * 60 * 60
RETRY_INTERVAL = 10 * 60

log = logging.getLogger(__name__)


async def refresh_schedule(schedule: FileSchedule, db: QueueDB) -> int:
    events = await schedule.two_weeks()
    await db.sync_schedule(events)
    return len(events)


async def refresh_loop(schedule: FileSchedule, db: QueueDB) -> None:
    while True:
        delay = REFRESH_INTERVAL
        try:
            log.info("Schedule refreshed: %s entries", await refresh_schedule(schedule, db))
        except Exception:
            delay = RETRY_INTERVAL
            log.exception("Schedule refresh failed; keeping cached schedule, retry in %s min", delay // 60)
        await asyncio.sleep(delay)
