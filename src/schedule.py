import hashlib
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import recurring_ical_events
from icalendar import Calendar

MOSCOW = ZoneInfo("Europe/Moscow")
log = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", value.casefold().replace("ё", "е"))


def _as_moscow(value) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=MOSCOW)
    if not isinstance(value, datetime):
        raise ValueError(f"Unsupported iCalendar date value: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW)
    return value.astimezone(MOSCOW)


def _property(component, *names) -> str | None:
    for name in names:
        value = component.get(name)
        if value:
            return str(value).strip()
    return None


class ScheduleClient:
    """Direct client for RTU MIREA's public schedule search and iCalendar endpoints."""

    def __init__(self, base_url: str = "https://schedule-of.mirea.ru", proxy: str | None = None):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=30, proxy=proxy, follow_redirects=True,
            headers={"Accept": "application/json, text/calendar, */*", "User-Agent": "MIREA-QueueBot/1.0"},
        )

    async def two_weeks(self, group: str) -> list[dict]:
        start = date.today()
        end = start + timedelta(days=14)
        search_response = await self.client.get(
            "/schedule/api/search", params={"match": group, "limit": 100}
        )
        search_response.raise_for_status()
        results = search_response.json().get("data", [])
        wanted = _normalize(group)
        groups = [item for item in results if item.get("scheduleTarget") == 1]
        selected = next(
            (item for item in groups if _normalize(str(item.get("targetTitle", ""))) == wanted),
            None,
        )
        if selected is None:
            selected = next(
                (item for item in groups if wanted in _normalize(str(item.get("targetTitle", "")))),
                None,
            )
        if selected is None:
            names = ", ".join(str(item.get("targetTitle", "?")) for item in groups[:5])
            raise ValueError(f"Группа «{group}» не найдена в API расписания. Варианты: {names or 'нет результатов'}")

        schedule_type = int(selected["scheduleTarget"])
        entity_id = int(selected["id"])
        response = await self.client.get(
            f"/schedule/api/ical/{schedule_type}/{entity_id}",
            params={"includeMeta": "true"},
            headers={"Accept": "text/calendar"},
        )
        response.raise_for_status()
        calendar = Calendar.from_ical(response.content)

        window_start = datetime.combine(start, time.min, tzinfo=MOSCOW)
        window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=MOSCOW)
        occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)
        events = []
        for item in occurrences:
            if str(item.get("STATUS", "")).upper() == "CANCELLED":
                continue
            dtstart = _as_moscow(item.decoded("DTSTART"))
            dtend_value = item.decoded("DTEND") if item.get("DTEND") else None
            dtend = _as_moscow(dtend_value) if dtend_value else None
            summary = _property(item, "SUMMARY") or "Занятие"
            lesson_type = _property(item, "X-LESSON-TYPE", "X-MIREA-LESSON-TYPE", "LESSON-TYPE")
            location = _property(item, "LOCATION", "X-ROOM", "X-AUDITORIUM")
            teacher = _property(item, "X-TEACHER", "X-TEACHERS", "TEACHER")
            description = _property(item, "DESCRIPTION")
            uid = _property(item, "UID") or hashlib.sha1(
                f"{dtstart.isoformat()}|{summary}".encode("utf-8")
            ).hexdigest()
            key = f"{dtstart.date().isoformat()}|{dtstart.strftime('%H:%M')}|{uid}"
            details = {
                "date": dtstart.date().isoformat(),
                "time_start": dtstart.strftime("%H:%M"),
                "time_end": dtend.strftime("%H:%M") if dtend else "",
                "discipline": summary,
                "lesson_type": lesson_type or "Занятие",
                "lesson_type_full": lesson_type or "Занятие",
                "teachers": [{"name": teacher}] if teacher else [],
                "groups": [{"name": group}],
                "room": {"number": location} if location else None,
                "description": description,
                "schedule_source": "schedule-of.mirea.ru",
            }
            events.append({
                "key": key,
                "date": details["date"],
                "starts": details["time_start"],
                "title": summary,
                "raw": json.dumps(details, ensure_ascii=False),
            })

        log.info("Fetched %s schedule entries for %s (%s)", len(events), group, selected.get("targetTitle"))
        return events

    async def close(self):
        await self.client.aclose()
