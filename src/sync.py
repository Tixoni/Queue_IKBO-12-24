import asyncio
import logging

import httpx

from src.db import QueueDB
from src.schedule import ScheduleClient

REFRESH_INTERVAL = 6 * 60 * 60
RETRY_INTERVAL = 10 * 60

log = logging.getLogger(__name__)


async def refresh_schedule(schedule: ScheduleClient, db: QueueDB, group: str) -> int:
    events = await schedule.two_weeks(group)
    await db.sync_schedule(events)
    return len(events)


async def refresh_loop(schedule: ScheduleClient, db: QueueDB, group: str, api_base: str) -> None:
    while True:
        delay = REFRESH_INTERVAL
        try:
            log.info("Schedule refreshed: %s entries", await refresh_schedule(schedule, db, group))
        except httpx.TransportError as exc:
            delay = RETRY_INTERVAL
            log.warning(
                "Cannot reach schedule API at %s (%s); timetable cache was kept, retry in %s min.",
                api_base, type(exc).__name__, delay // 60,
            )
        except Exception:
            delay = RETRY_INTERVAL
            log.exception("Schedule refresh failed; keeping cached schedule, retry in %s min", delay // 60)
        await asyncio.sleep(delay)
