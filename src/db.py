"""PostgreSQL storage (asyncpg). Uniqueness constraints and transactions keep queues consistent
when many people press buttons at once or several bot instances share one database."""
from contextlib import asynccontextmanager
from datetime import date, timedelta

import asyncpg

BOOKING_DAYS = 14
JOINED = "Вы добавлены в очередь."
DUPLICATE_TOPIC_LIST = -1
TOPIC_LIST_FULL = -1
MAX_TOPICS_PER_LIST = 60

SCHEDULE_COLUMNS = "id,event_key,event_date,starts,title,raw"

# Dates are stored as ISO strings (YYYY-MM-DD): they sort and compare correctly as text
# and the rest of the bot works with them as strings.
SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_date TEXT NOT NULL,
    starts TEXT,
    title TEXT NOT NULL,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule(event_date);

CREATE TABLE IF NOT EXISTS deadlines (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_date TEXT NOT NULL,
    title TEXT NOT NULL,
    capacity INTEGER,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    event_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS registrations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deadline_id BIGINT NOT NULL REFERENCES deadlines(id),
    user_id BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (deadline_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_registrations_user ON registrations(user_id);

CREATE TABLE IF NOT EXISTS occupied_topics (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deadline_id BIGINT NOT NULL REFERENCES deadlines(id),
    topic TEXT NOT NULL,
    UNIQUE (deadline_id, topic)
);

CREATE TABLE IF NOT EXISTS topic_lists (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deadline_id BIGINT NOT NULL REFERENCES deadlines(id),
    event_key TEXT NOT NULL,
    event_date TEXT NOT NULL,
    subject TEXT NOT NULL,
    title TEXT NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_key, title)
);

CREATE TABLE IF NOT EXISTS topic_list_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic_list_id BIGINT NOT NULL REFERENCES topic_lists(id),
    topic TEXT NOT NULL,
    UNIQUE (topic_list_id, topic)
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id BIGINT PRIMARY KEY,
    display_name TEXT NOT NULL
);
"""


def booking_window() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=BOOKING_DAYS)


def in_booking_window(day: date) -> bool:
    start, end = booking_window()
    return start <= day <= end


def _window() -> tuple[str, str]:
    start, end = booking_window()
    return start.isoformat(), end.isoformat()


class QueueDB:
    def __init__(self, dsn: str, server_settings: dict[str, str] | None = None):
        self.dsn = dsn
        self.server_settings = server_settings
        self._pool: asyncpg.Pool | None = None

    async def initialize(self):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=1, max_size=5, server_settings=self.server_settings
            )
        async with self._pool.acquire() as conn:
            # Serialize schema creation when several instances start at once.
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(724001)")
                await conn.execute(SCHEMA)

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("QueueDB.initialize() was not called")
        return self._pool

    @asynccontextmanager
    async def transaction(self):
        """Connection inside a transaction: commits on normal exit, rolls back on error."""
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def _all(self, sql: str, *args) -> list[dict]:
        return [dict(row) for row in await self.pool.fetch(sql, *args)]

    async def _one(self, sql: str, *args) -> dict | None:
        row = await self.pool.fetchrow(sql, *args)
        return dict(row) if row else None

    # --- schedule -----------------------------------------------------------

    async def sync_schedule(self, events: list[dict]):
        """Make the booking window match `events`. Rows are upserted, so lesson ids stay stable."""
        start, end = _window()
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM schedule WHERE event_date BETWEEN $1 AND $2 AND NOT (event_key = ANY($3::text[]))",
                start, end, [e["key"] for e in events],
            )
            await conn.executemany(
                "INSERT INTO schedule(event_key,event_date,starts,title,raw) VALUES($1,$2,$3,$4,$5) "
                "ON CONFLICT (event_key) DO UPDATE SET event_date=excluded.event_date, "
                "starts=excluded.starts, title=excluded.title, raw=excluded.raw",
                [(e["key"], e["date"], e.get("starts"), e["title"], e.get("raw")) for e in events],
            )

    async def has_schedule(self) -> bool:
        start, end = _window()
        return await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM schedule WHERE event_date BETWEEN $1 AND $2)", start, end
        )

    async def list_schedule_for_date(self, event_date: str) -> list[dict]:
        return await self._all(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE event_date=$1 ORDER BY starts,title", event_date
        )

    async def get_schedule_entry(self, entry_id: int) -> dict | None:
        return await self._one(f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE id=$1", entry_id)

    async def get_schedule_by_event_key(self, event_key: str) -> dict | None:
        return await self._one(f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE event_key=$1", event_key)

    # --- queues -------------------------------------------------------------

    async def get_deadline(self, deadline_id: int) -> dict | None:
        return await self._one(
            "SELECT id,event_date,title,event_key FROM deadlines WHERE id=$1 AND active", deadline_id
        )

    async def get_or_create_schedule_queue(self, event_key: str, event_date: str, title: str) -> int:
        """Return the persistent queue ID for a schedule event, creating it on first use."""
        async with self.transaction() as conn:
            queue_id = await conn.fetchval(
                "INSERT INTO deadlines(event_date,title,event_key) VALUES($1,$2,$3) "
                "ON CONFLICT (event_key) DO NOTHING RETURNING id",
                event_date, title, event_key,
            )
            if queue_id is None:
                queue_id = await conn.fetchval("SELECT id FROM deadlines WHERE event_key=$1", event_key)
            return queue_id

    async def join(self, deadline_id: int, user_id: int, name: str) -> str:
        async with self.transaction() as conn:
            deadline = await conn.fetchrow("SELECT event_date,active FROM deadlines WHERE id=$1", deadline_id)
            if not deadline or not deadline["active"]:
                return "Очередь не найдена или запись закрыта."
            if not in_booking_window(date.fromisoformat(deadline["event_date"])):
                return "Запись доступна только на пары в ближайшие 2 недели."
            inserted = await conn.fetchval(
                "INSERT INTO registrations(deadline_id,user_id,display_name) VALUES($1,$2,$3) "
                "ON CONFLICT (deadline_id,user_id) DO NOTHING RETURNING id",
                deadline_id, user_id, name,
            )
            return JOINED if inserted else "Вы уже записаны."

    async def leave(self, deadline_id: int, user_id: int) -> bool:
        deleted = await self.pool.fetchval(
            "DELETE FROM registrations WHERE deadline_id=$1 AND user_id=$2 RETURNING id", deadline_id, user_id
        )
        return deleted is not None

    async def add_user(self, deadline_id: int, user_id: int, name: str) -> bool:
        async with self.transaction() as conn:
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM deadlines WHERE id=$1 AND active)", deadline_id):
                return False
            inserted = await conn.fetchval(
                "INSERT INTO registrations(deadline_id,user_id,display_name) VALUES($1,$2,$3) "
                "ON CONFLICT (deadline_id,user_id) DO NOTHING RETURNING id",
                deadline_id, user_id, name,
            )
            return inserted is not None

    async def get_queue(self, deadline_id: int) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT display_name FROM registrations WHERE deadline_id=$1 ORDER BY id", deadline_id
        )
        return [row["display_name"] for row in rows]

    async def is_registered(self, deadline_id: int, user_id: int) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM registrations WHERE deadline_id=$1 AND user_id=$2)", deadline_id, user_id
        )

    async def get_user_registrations(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT d.id,d.event_date AS date,d.title FROM registrations r "
            "JOIN deadlines d ON d.id=r.deadline_id "
            "WHERE r.user_id=$1 AND d.active ORDER BY d.event_date,d.id",
            user_id,
        )

    # --- topics -------------------------------------------------------------

    async def get_topics(self, deadline_id: int) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT topic FROM occupied_topics WHERE deadline_id=$1 ORDER BY topic", deadline_id
        )
        return [row["topic"] for row in rows]

    async def create_topic_list(
        self,
        deadline_id: int,
        event_key: str,
        event_date: str,
        subject: str,
        title: str,
        topics: list[str],
        created_by: int,
    ) -> int | None:
        """Return the new list id, DUPLICATE_TOPIC_LIST if the title is taken, None if the queue is gone."""
        async with self.transaction() as conn:
            queue = await conn.fetchrow(
                "SELECT event_key,event_date FROM deadlines WHERE id=$1 AND active", deadline_id
            )
            if not queue or queue["event_key"] != event_key or queue["event_date"] != event_date:
                return None
            if not in_booking_window(date.fromisoformat(event_date)):
                return None
            list_id = await conn.fetchval(
                "INSERT INTO topic_lists(deadline_id,event_key,event_date,subject,title,created_by) "
                "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (event_key,title) DO NOTHING RETURNING id",
                deadline_id, event_key, event_date, subject, title, created_by,
            )
            if list_id is None:
                return DUPLICATE_TOPIC_LIST
            await conn.executemany(
                "INSERT INTO topic_list_items(topic_list_id,topic) VALUES($1,$2) ON CONFLICT DO NOTHING",
                [(list_id, topic) for topic in topics],
            )
            return list_id

    async def get_topic_list(self, topic_list_id: int) -> dict | None:
        return await self._one(
            "SELECT l.id,l.title,l.deadline_id FROM topic_lists l "
            "JOIN deadlines d ON d.id=l.deadline_id WHERE l.id=$1 AND d.active",
            topic_list_id,
        )

    async def add_topics(self, topic_list_id: int, topics: list[str]) -> int | None:
        """Append topics to a list. Returns how many were new, TOPIC_LIST_FULL if over the cap,
        None if the list or its pair is gone."""
        async with self.transaction() as conn:
            # FOR UPDATE: two people extending the same list at once cannot exceed the cap together.
            row = await conn.fetchrow(
                "SELECT l.event_date FROM topic_lists l JOIN deadlines d ON d.id=l.deadline_id "
                "WHERE l.id=$1 AND d.active FOR UPDATE OF l",
                topic_list_id,
            )
            if not row or not in_booking_window(date.fromisoformat(row["event_date"])):
                return None
            existing = {
                r["topic"] for r in await conn.fetch(
                    "SELECT topic FROM topic_list_items WHERE topic_list_id=$1", topic_list_id
                )
            }
            new = [topic for topic in topics if topic not in existing]
            if len(existing) + len(new) > MAX_TOPICS_PER_LIST:
                return TOPIC_LIST_FULL
            await conn.executemany(
                "INSERT INTO topic_list_items(topic_list_id,topic) VALUES($1,$2) ON CONFLICT DO NOTHING",
                [(topic_list_id, topic) for topic in new],
            )
            return len(new)

    async def get_topic_lists(self, deadline_id: int) -> list[dict]:
        lists = await self._all(
            "SELECT l.id,l.title,l.subject,l.event_date,l.created_by, "
            "COALESCE(array_agg(i.topic ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL), '{}') AS topics "
            "FROM topic_lists l LEFT JOIN topic_list_items i ON i.topic_list_id=l.id "
            "WHERE l.deadline_id=$1 GROUP BY l.id ORDER BY l.id",
            deadline_id,
        )
        for topic_list in lists:
            topic_list["topics"] = list(topic_list["topics"])
        return lists

    # --- profiles -----------------------------------------------------------

    async def get_user_name(self, user_id: int) -> str | None:
        return await self.pool.fetchval("SELECT display_name FROM profiles WHERE user_id=$1", user_id)

    async def set_user_name(self, user_id: int, name: str):
        await self.pool.execute(
            "INSERT INTO profiles(user_id,display_name) VALUES($1,$2) "
            "ON CONFLICT (user_id) DO UPDATE SET display_name=excluded.display_name",
            user_id, name,
        )
