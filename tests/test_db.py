import asyncio

from src.db import JOINED

from tests.conftest import day, schedule_event


async def make_queue(db, offset: int = 1, key: str = "k1") -> int:
    return await db.get_or_create_schedule_queue(key, day(offset), "Математика")


# --- schedule cache ---------------------------------------------------------------


async def test_sync_schedule_replaces_window(db):
    await db.sync_schedule([schedule_event("a", day(1)), schedule_event("b", day(2))])
    await db.sync_schedule([schedule_event("c", day(1), starts="11:00")])

    assert [row["event_key"] for row in await db.list_schedule_for_date(day(1))] == ["c"]
    assert await db.list_schedule_for_date(day(2)) == []
    assert await db.has_schedule()


async def test_sync_schedule_keeps_rows_outside_window(db):
    await db.sync_schedule([schedule_event("old", day(-3))])
    await db.sync_schedule([])

    assert [row["event_key"] for row in await db.list_schedule_for_date(day(-3))] == ["old"]
    assert not await db.has_schedule()


async def test_lesson_ids_survive_resync(db):
    """Buttons carry the lesson id, so a periodic refresh must not renumber lessons."""
    await db.sync_schedule([schedule_event("a", day(1)), schedule_event("b", day(1), starts="11:00")])
    before = {row["event_key"]: row["id"] for row in await db.list_schedule_for_date(day(1))}
    await db.sync_schedule([schedule_event("b", day(1), starts="11:00", title="Новое"), schedule_event("a", day(1))])
    after = {row["event_key"]: (row["id"], row["title"]) for row in await db.list_schedule_for_date(day(1))}

    assert after == {"a": (before["a"], "Математика"), "b": (before["b"], "Новое")}


async def test_telegram_ids_above_32_bits(db):
    queue_id = await make_queue(db)
    big_id = 8_817_961_121
    assert await db.join(queue_id, big_id, "Бот") == JOINED
    await db.set_user_name(big_id, "Имя")
    assert await db.is_registered(queue_id, big_id)
    assert await db.get_user_name(big_id) == "Имя"


async def test_schedule_lookups(db):
    await db.sync_schedule([schedule_event("a", day(1), starts="12:00"), schedule_event("b", day(1), starts="09:00")])
    rows = await db.list_schedule_for_date(day(1))

    assert [row["starts"] for row in rows] == ["09:00", "12:00"]
    assert (await db.get_schedule_entry(rows[0]["id"]))["event_key"] == "b"
    assert (await db.get_schedule_by_event_key("a"))["starts"] == "12:00"
    assert await db.get_schedule_entry(9999) is None


# --- queues -------------------------------------------------------------------------


async def test_queue_is_created_once_per_event(db):
    first = await make_queue(db)
    assert await make_queue(db) == first
    assert await make_queue(db, key="k2") != first


async def test_join_and_leave(db):
    queue_id = await make_queue(db)

    assert await db.join(queue_id, 10, "Вася") == JOINED
    assert await db.join(queue_id, 10, "Вася") == "Вы уже записаны."
    assert await db.join(queue_id, 11, "Петя") == JOINED
    assert await db.get_queue(queue_id) == ["Вася", "Петя"]
    assert await db.is_registered(queue_id, 10)

    assert await db.leave(queue_id, 10)
    assert not await db.leave(queue_id, 10)
    assert await db.get_queue(queue_id) == ["Петя"]


async def test_join_respects_booking_window(db):
    past = await make_queue(db, offset=-1, key="past")
    far = await make_queue(db, offset=15, key="far")
    edge = await make_queue(db, offset=14, key="edge")

    assert "2 недели" in await db.join(past, 1, "A")
    assert "2 недели" in await db.join(far, 1, "A")
    assert await db.join(edge, 1, "A") == JOINED


async def test_join_unknown_or_closed_queue(db):
    queue_id = await make_queue(db)
    async with db.transaction() as conn:
        await conn.execute("UPDATE deadlines SET active=FALSE WHERE id=$1", queue_id)

    assert "не найдена" in await db.join(queue_id, 1, "A")
    assert "не найдена" in await db.join(9999, 1, "A")
    assert await db.get_deadline(queue_id) is None


async def test_concurrent_joins_keep_one_registration(db):
    queue_id = await make_queue(db)
    results = await asyncio.gather(*(db.join(queue_id, 7, "Дубль") for _ in range(10)))

    assert results.count(JOINED) == 1
    assert await db.get_queue(queue_id) == ["Дубль"]


async def test_concurrent_queue_creation_returns_one_id(db):
    ids = await asyncio.gather(*(make_queue(db) for _ in range(10)))
    assert len(set(ids)) == 1


async def test_queue_order_follows_join_order(db):
    queue_id = await make_queue(db)
    for user_id in range(5):
        await db.join(queue_id, user_id, f"u{user_id}")

    assert await db.get_queue(queue_id) == [f"u{i}" for i in range(5)]


async def test_admin_add_user(db):
    queue_id = await make_queue(db)

    assert await db.add_user(queue_id, 5, "Иван")
    assert not await db.add_user(queue_id, 5, "Иван")
    assert not await db.add_user(9999, 5, "Иван")


async def test_user_registrations(db):
    q1 = await make_queue(db, offset=2, key="a")
    q2 = await make_queue(db, offset=1, key="b")
    await db.join(q1, 5, "Иван")
    await db.join(q2, 5, "Иван")

    assert [row["id"] for row in await db.get_user_registrations(5)] == [q2, q1]
    assert await db.get_user_registrations(6) == []


async def test_sql_injection_is_stored_as_text(db):
    queue_id = await make_queue(db)
    evil = "x'); DROP TABLE registrations; --"
    await db.join(queue_id, 1, evil)
    await db.set_user_name(1, evil)

    assert await db.get_queue(queue_id) == [evil]
    assert await db.get_user_name(1) == evil


# --- profiles ------------------------------------------------------------------------------


async def test_profile_name_upsert(db):
    assert await db.get_user_name(1) is None
    await db.set_user_name(1, "Первое")
    await db.set_user_name(1, "Второе")
    assert await db.get_user_name(1) == "Второе"


async def test_initialize_is_idempotent(db):
    await db.initialize()
    await db.initialize()
    assert await make_queue(db) > 0
