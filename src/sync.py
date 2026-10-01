import asyncio
import logging

import httpx

from src.db import QueueDB
from src.schedule import ScheduleClient

REFRESH_INTERVAL = 6 * 60 * 60

log = logging.getLogger(__name__)


async def refresh_schedule(schedule: ScheduleClient, db: QueueDB, group: str) -> int:
    events = await schedule.two_weeks(group)
    await db.sync_schedule(events)
    return len(events)


async def refresh_loop(schedule: ScheduleClient, db: QueueDB, group: str, api_base: str) -> None:
    while True:
        try:
            log.info("Schedule refreshed: %s entries", await refresh_schedule(schedule, db, group))
        except httpx.ConnectError:
            log.warning("Cannot connect to schedule API at %s; timetable cache was kept.", api_base)
        except Exception:
            log.exception("Schedule refresh failed; keeping cached schedule")
        await asyncio.sleep(REFRESH_INTERVAL)
