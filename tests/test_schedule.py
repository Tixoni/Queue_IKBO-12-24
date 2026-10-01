"""Timetable parsing (synthetic and the real bundled file), downloading on a mocked API, refresh loop."""
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from src import sync
from src.config import settings
from src.schedule import FileSchedule, download_calendar, parse_calendar

BUNDLED = Path(settings.schedule_file)


def ical(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def vevent(uid: str, start: date, summary: str, extra: str = "") -> str:
    stamp = start.strftime("%Y%m%d")
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:{stamp}T000000Z\r\n"
        f"DTSTART;TZID=Europe/Moscow:{stamp}T090000\r\nDTEND;TZID=Europe/Moscow:{stamp}T103000\r\n"
        f"SUMMARY:{summary}\r\n{extra}"
        "END:VEVENT\r\n"
    )


def details(event: dict) -> dict:
    return json.loads(event["raw"])


# --- parse_calendar on synthetic data -------------------------------------------------------


def test_meta_fields_are_used():
    start = date(2026, 10, 5)
    calendar = ical(vevent("a", start, "ПР Моделирование", (
        "X-META-DISCIPLINE:Моделирование бизнес-процессов\r\n"
        "X-META-LESSON_TYPE:ПР\r\nX-META-FULL_LESSON_TYPE:Практические занятия\r\n"
        "X-META-TEACHER;ID=1:Иванов Иван\r\nX-META-TEACHER;ID=2:Петров Пётр\r\n"
        "X-META-AUDITORIUM;NUMBER=А-424-2;CAMPUS=В-78:А-424-2 (В-78)\r\n"
    )))
    [event] = parse_calendar(calendar, "ИКБО-12-24", start)

    assert event["key"] == "2026-10-05|09:00|a"
    assert event["title"] == "ПР Моделирование"
    data = details(event)
    assert data["discipline"] == "Моделирование бизнес-процессов"
    assert data["lesson_type"] == "ПР"
    assert data["teachers"] == [{"name": "Иванов Иван"}, {"name": "Петров Пётр"}]
    assert data["room"] == {"number": "А-424-2", "campus": "В-78"}
    assert (data["time_start"], data["time_end"]) == ("09:00", "10:30")


def test_fallbacks_without_meta():
    start = date(2026, 10, 5)
    calendar = ical(vevent("a", start, "ЛК Физика", (
        "CATEGORIES:ЛК\r\nLOCATION:Г-112\r\nDESCRIPTION:Преподаватель: Сидоров С.С.\\n\\nГруппы:\\nX\r\n"
    )))
    data = details(parse_calendar(calendar, "g", start)[0])

    assert (data["discipline"], data["lesson_type"]) == ("Физика", "ЛК")
    assert data["teachers"] == [{"name": "Сидоров С.С."}]
    assert data["room"] == {"number": "Г-112"}


def test_week_markers_cancelled_and_out_of_window_are_skipped():
    start = date(2026, 10, 5)
    marker = (
        "BEGIN:VEVENT\r\nUID:w\r\nDTSTAMP:20261005T000000Z\r\n"
        "DTSTART;VALUE=DATE:20261005\r\nDTEND;VALUE=DATE:20261012\r\nSUMMARY:6 неделя\r\nEND:VEVENT\r\n"
    )
    calendar = ical(
        marker,
        vevent("ok", start, "ЛК Пара"),
        vevent("cancel", start + timedelta(days=1), "ЛК Отмена", "STATUS:CANCELLED\r\n"),
        vevent("far", start + timedelta(days=15), "ЛК Далеко"),
    )
    assert [event["title"] for event in parse_calendar(calendar, "g", start)] == ["ЛК Пара"]


def test_biweekly_rule_with_exdate():
    start = date(2026, 10, 5)
    calendar = ical(vevent("bi", start, "ЛК Раз в две недели", (
        "RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL=20261230T210000Z\r\n"
        "EXDATE;TZID=Europe/Moscow:20261019T090000\r\n"
    )))
    window = parse_calendar(calendar, "g", start, days=42)

    assert [event["date"] for event in window] == ["2026-10-05", "2026-11-02", "2026-11-16"]
    assert parse_calendar(calendar, "g", date(2027, 1, 4)) == []  # after UNTIL


# --- the real bundled timetable --------------------------------------------------------------


def test_bundled_file_matches_known_lessons():
    lessons = parse_calendar(BUNDLED.read_bytes(), "ИКБО-12-24", date(2026, 10, 5), days=13)
    monday = [details(e) for e in lessons if e["date"] == "2026-10-05"]

    assert len(lessons) == 47
    assert ("12:40", "14:10", "ПР", "Моделирование бизнес-процессов") in {
        (d["time_start"], d["time_end"], d["lesson_type"], d["discipline"]) for d in monday
    }
    practice = next(d for d in monday if d["time_start"] == "12:40")
    assert practice["room"] == {"number": "А-424-2", "campus": "В-78"}
    assert practice["teachers"] == [{"name": "Геращенко Людмила Андреевна"}]
    assert all("неделя" not in e["title"] for e in lessons)


def test_bundled_file_repeats_every_two_weeks():
    content = BUNDLED.read_bytes()

    def lessons_of(day: date) -> list[tuple]:
        return [(e["starts"], e["title"]) for e in parse_calendar(content, "g", day, days=0)]

    for offset in range(14):
        day = date(2026, 10, 5) + timedelta(days=offset)
        assert lessons_of(day) == lessons_of(day + timedelta(days=14)), day


async def test_file_schedule_uses_today(tmp_path):
    path = tmp_path / "g.ics"
    path.write_bytes(ical(vevent("t", date.today() + timedelta(days=1), "ЛК Завтра")))
    assert [e["title"] for e in await FileSchedule(path, "g").two_weeks()] == ["ЛК Завтра"]


# --- download_calendar (used by tools/update_schedule.py) ---------------------------------------


def fake_api(groups: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/schedule/api/search":
            return httpx.Response(200, json={"data": groups})
        if request.url.path == "/schedule/api/ical/1/4783":
            assert request.url.params["includeMeta"] == "true"
            return httpx.Response(200, content=b"BEGIN:VCALENDAR")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_download_picks_exact_group():
    groups = [
        {"scheduleTarget": 2, "id": 1, "targetTitle": "ИКБО-12-24"},  # a teacher/room with the same name
        {"scheduleTarget": 1, "id": 9, "targetTitle": "ИКБО-12-24 (подгруппа)"},
        {"scheduleTarget": 1, "id": 4783, "targetTitle": "ИКБО-12-24"},
    ]
    assert await download_calendar("икбо-12-24", "https://fake", fake_api(groups)) == b"BEGIN:VCALENDAR"


async def test_download_unknown_group():
    with pytest.raises(ValueError, match="не найдена"):
        await download_calendar("ИКБО-12-24", "https://fake", fake_api([]))


# --- refresh loop ------------------------------------------------------------------------------


class StopLoop(Exception):
    pass


class FakeDB:
    def __init__(self):
        self.synced = None

    async def sync_schedule(self, events):
        self.synced = events


class FakeSchedule:
    def __init__(self, result):
        self.result = result

    async def two_weeks(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def run_one_iteration(monkeypatch, schedule, db=None) -> list[float]:
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        raise StopLoop

    monkeypatch.setattr(sync.asyncio, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        await sync.refresh_loop(schedule, db or FakeDB())
    return delays


async def test_refresh_loop_syncs_and_waits(monkeypatch):
    db = FakeDB()
    assert await run_one_iteration(monkeypatch, FakeSchedule([{"key": "x"}]), db) == [sync.REFRESH_INTERVAL]
    assert db.synced == [{"key": "x"}]


async def test_refresh_loop_retries_soon_after_failure(monkeypatch, caplog):
    assert await run_one_iteration(monkeypatch, FakeSchedule(FileNotFoundError("no file"))) == [sync.RETRY_INTERVAL]
    assert "retry in 10 min" in caplog.text
