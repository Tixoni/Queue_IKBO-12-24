"""ScheduleClient against a fake MIREA API (httpx.MockTransport) and the refresh loop's retry policy."""
import json
from datetime import date, timedelta

import httpx
import pytest

from src import sync
from src.schedule import ScheduleClient


def ical(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def vevent(uid: str, start: date, summary: str, extra: str = "") -> str:
    stamp = start.strftime("%Y%m%d")
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:{stamp}T000000Z\r\n"
        f"DTSTART;TZID=Europe/Moscow:{stamp}T090000\r\nDTEND;TZID=Europe/Moscow:{stamp}T103000\r\n"
        f"SUMMARY:{summary}\r\nLOCATION:А-424-2 (В-78)\r\nX-TEACHER:Иванов И.И.\r\n{extra}"
        "END:VEVENT\r\n"
    )


def fake_api(calendar: bytes, groups: list[dict] | None = None) -> httpx.MockTransport:
    groups = groups if groups is not None else [{"scheduleTarget": 1, "id": 42, "targetTitle": "ИКБО-12-24"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/schedule/api/search":
            return httpx.Response(200, json={"data": groups})
        if request.url.path == "/schedule/api/ical/1/42":
            return httpx.Response(200, content=calendar, headers={"Content-Type": "text/calendar"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_two_weeks_parses_events():
    tomorrow = date.today() + timedelta(days=1)
    calendar = ical(
        vevent("a", tomorrow, "ПР Моделирование"),
        vevent("b", tomorrow, "Отменённая пара", "STATUS:CANCELLED\r\n"),
        vevent("c", date.today() + timedelta(days=30), "Слишком далеко"),
    )
    client = ScheduleClient("https://fake", transport=fake_api(calendar))
    try:
        events = await client.two_weeks("ИКБО-12-24")
    finally:
        await client.close()

    assert [event["title"] for event in events] == ["ПР Моделирование"]
    [event] = events
    details = json.loads(event["raw"])
    assert event["date"] == tomorrow.isoformat()
    assert (details["time_start"], details["time_end"]) == ("09:00", "10:30")
    assert details["room"] == {"number": "А-424-2 (В-78)"}
    assert details["teachers"] == [{"name": "Иванов И.И."}]


async def test_two_weeks_expands_weekly_recurrence():
    start = date.today() + timedelta(days=1)
    calendar = ical(vevent("weekly", start, "Лекция", "RRULE:FREQ=WEEKLY;COUNT=5\r\n"))
    client = ScheduleClient("https://fake", transport=fake_api(calendar))
    try:
        events = await client.two_weeks("ИКБО-12-24")
    finally:
        await client.close()

    assert [event["date"] for event in events] == [(start + timedelta(weeks=w)).isoformat() for w in range(2)]
    assert len({event["key"] for event in events}) == 2


async def test_unknown_group_is_reported():
    client = ScheduleClient("https://fake", transport=fake_api(ical(), groups=[]))
    try:
        with pytest.raises(ValueError, match="не найдена"):
            await client.two_weeks("ИКБО-12-24")
    finally:
        await client.close()


class StopLoop(Exception):
    pass


async def run_one_iteration(monkeypatch, schedule) -> list[float]:
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        raise StopLoop

    monkeypatch.setattr(sync.asyncio, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        await sync.refresh_loop(schedule, db=FakeDB(), group="ИКБО-12-24", api_base="https://fake")
    return delays


class FakeDB:
    def __init__(self):
        self.synced = None

    async def sync_schedule(self, events):
        self.synced = events


class FakeSchedule:
    def __init__(self, result):
        self.result = result

    async def two_weeks(self, group):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def test_refresh_loop_waits_long_after_success(monkeypatch):
    assert await run_one_iteration(monkeypatch, FakeSchedule([])) == [sync.REFRESH_INTERVAL]


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("timeout"), ValueError("bad data")])
async def test_refresh_loop_retries_soon_after_failure(monkeypatch, error, caplog):
    assert await run_one_iteration(monkeypatch, FakeSchedule(error)) == [sync.RETRY_INTERVAL]
    assert "retry in 10 min" in caplog.text
