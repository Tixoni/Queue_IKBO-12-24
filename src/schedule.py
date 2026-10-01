"""Group timetable from an iCalendar file of RTU MIREA (schedule-of.mirea.ru).

The bot reads a bundled .ics file (see SCHEDULE_FILE): the university site answers only from some
Russian networks, so the hosted bot never calls it. tools/update_schedule.py refreshes the file.
"""
import hashlib
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import recurring_ical_events
from icalendar import Calendar

from src.db import BOOKING_DAYS

MOSCOW = ZoneInfo("Europe/Moscow")
log = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", value.casefold().replace("ё", "е"))


def _as_moscow(value: datetime) -> datetime:
    return value.replace(tzinfo=MOSCOW) if value.tzinfo is None else value.astimezone(MOSCOW)


def _text(component, *names: str) -> str | None:
    for name in names:
        value = component.get(name)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            # CATEGORIES is a vCategory object, everything else is text-like
            value = ", ".join(map(str, value.cats)) if hasattr(value, "cats") else str(value)
            if value.strip():
                return value.strip()
    return None


def _all(component, name: str) -> list:
    value = component.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _room(item) -> dict | None:
    for prop in _all(item, "X-META-AUDITORIUM"):
        number, campus = prop.params.get("NUMBER"), prop.params.get("CAMPUS")
        if number:
            return {"number": str(number), **({"campus": str(campus)} if campus else {})}
    location = _text(item, "LOCATION")
    return {"number": location} if location else None


def _teachers(item) -> list[dict]:
    names = [str(prop).strip() for prop in _all(item, "X-META-TEACHER") if str(prop).strip()]
    if not names:
        match = re.search(r"Преподаватель:\s*([^\n]+)", _text(item, "DESCRIPTION") or "")
        names = [match.group(1).strip()] if match else []
    return [{"name": name} for name in names]


def parse_calendar(content: bytes, group: str, start: date, days: int = BOOKING_DAYS) -> list[dict]:
    """Lessons from `start` to `start + days` inclusive, in the format QueueDB.sync_schedule expects."""
    calendar = Calendar.from_ical(content)
    window_start = datetime.combine(start, time.min, tzinfo=MOSCOW)
    window_end = datetime.combine(start + timedelta(days=days + 1), time.min, tzinfo=MOSCOW)

    events = []
    for item in recurring_ical_events.of(calendar).between(window_start, window_end):
        dtstart = item.decoded("DTSTART")
        # All-day items ("5 неделя") are week markers, not lessons.
        if not isinstance(dtstart, datetime) or str(item.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = _as_moscow(dtstart)
        dtend = _as_moscow(item.decoded("DTEND")) if item.get("DTEND") else None
        summary = _text(item, "SUMMARY") or "Занятие"
        lesson_type = _text(item, "X-META-LESSON_TYPE", "CATEGORIES")
        discipline = _text(item, "X-META-DISCIPLINE")
        if not discipline:
            has_prefix = lesson_type and summary.startswith(f"{lesson_type} ")
            discipline = summary[len(lesson_type):].strip() if has_prefix else summary
        uid = _text(item, "UID") or hashlib.sha1(f"{dtstart.isoformat()}|{summary}".encode()).hexdigest()

        details = {
            "date": dtstart.date().isoformat(),
            "time_start": dtstart.strftime("%H:%M"),
            "time_end": dtend.strftime("%H:%M") if dtend else "",
            "discipline": discipline,
            "lesson_type": lesson_type or "Занятие",
            "lesson_type_full": _text(item, "X-META-FULL_LESSON_TYPE") or lesson_type or "Занятие",
            "teachers": _teachers(item),
            "groups": [{"name": group}],
            "room": _room(item),
        }
        events.append({
            "key": f"{details['date']}|{details['time_start']}|{uid}",
            "date": details["date"],
            "starts": details["time_start"],
            "title": summary,
            "raw": json.dumps(details, ensure_ascii=False),
        })
    events.sort(key=lambda event: (event["date"], event["starts"], event["title"]))
    return events


class FileSchedule:
    """Timetable read from a local .ics file; no network access."""

    def __init__(self, path: Path, group: str):
        self.path = path
        self.group = group

    async def two_weeks(self) -> list[dict]:
        events = parse_calendar(self.path.read_bytes(), self.group, date.today())
        log.info("Loaded %s lessons for %s from %s", len(events), self.group, self.path.name)
        return events


async def download_calendar(
    group: str,
    base_url: str = "https://schedule-of.mirea.ru",
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Fetch the group's iCalendar from the public MIREA API (works only from some Russian networks)."""
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), timeout=30, follow_redirects=True, transport=transport,
        headers={"Accept": "application/json, text/calendar, */*", "User-Agent": "MIREA-QueueBot/1.0"},
    ) as client:
        search = await client.get("/schedule/api/search", params={"match": group, "limit": 100})
        search.raise_for_status()
        wanted = _normalize(group)
        groups = [item for item in search.json().get("data", []) if item.get("scheduleTarget") == 1]
        selected = (
            next((g for g in groups if _normalize(str(g.get("targetTitle", ""))) == wanted), None)
            or next((g for g in groups if wanted in _normalize(str(g.get("targetTitle", "")))), None)
        )
        if selected is None:
            names = ", ".join(str(g.get("targetTitle", "?")) for g in groups[:5])
            raise ValueError(f"Группа «{group}» не найдена в API расписания. Варианты: {names or 'нет результатов'}")

        response = await client.get(
            f"/schedule/api/ical/{int(selected['scheduleTarget'])}/{int(selected['id'])}",
            params={"includeMeta": "true"},
            headers={"Accept": "text/calendar"},
        )
        response.raise_for_status()
        return response.content
