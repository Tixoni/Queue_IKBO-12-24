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

CREATE TABLE IF NOT EXISTS subjects (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS subject_lists (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id BIGINT NOT NULL REFERENCES subjects(id),
    title TEXT NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_id, title)
);

CREATE TABLE IF NOT EXISTS subject_list_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    list_id BIGINT NOT NULL REFERENCES subject_lists(id),
    topic TEXT NOT NULL,
    UNIQUE (list_id, topic)
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

    async def rename_queue(self, deadline_id: int, title: str) -> bool:
        updated = await self.pool.fetchval(
            "UPDATE deadlines SET title=$2 WHERE id=$1 AND active RETURNING id", deadline_id, title
        )
        return updated is not None

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

    # --- subjects and topic lists -------------------------------------------

    async def list_window_schedule(self) -> list[dict]:
        start, end = _window()
        return await self._all(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE event_date BETWEEN $1 AND $2", start, end
        )

    async def ensure_subject(self, name: str) -> int:
        async with self.transaction() as conn:
            subject_id = await conn.fetchval(
                "INSERT INTO subjects(name) VALUES($1) ON CONFLICT (name) DO NOTHING RETURNING id", name
            )
            return subject_id or await conn.fetchval("SELECT id FROM subjects WHERE name=$1", name)

    async def list_subjects(self, current: list[str]) -> list[dict]:
        """Subjects from the current timetable plus any subject that already has lists, with list counts."""
        async with self.transaction() as conn:
            await conn.executemany(
                "INSERT INTO subjects(name) VALUES($1) ON CONFLICT (name) DO NOTHING", [(n,) for n in current]
            )
            rows = await conn.fetch(
                "SELECT s.id, s.name, count(l.id) AS lists FROM subjects s "
                "LEFT JOIN subject_lists l ON l.subject_id=s.id "
                "GROUP BY s.id HAVING s.name = ANY($1::text[]) OR count(l.id) > 0 ORDER BY s.name",
                current,
            )
            return [dict(row) for row in rows]

    async def get_subject(self, subject_id: int) -> dict | None:
        return await self._one("SELECT id,name FROM subjects WHERE id=$1", subject_id)

    async def create_topic_list(self, subject_id: int, title: str, topics: list[str], created_by: int) -> int | None:
        """Return the new list id, DUPLICATE_TOPIC_LIST if the subject already has this title,
        None if the subject does not exist."""
        async with self.transaction() as conn:
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM subjects WHERE id=$1)", subject_id):
                return None
            list_id = await conn.fetchval(
                "INSERT INTO subject_lists(subject_id,title,created_by) VALUES($1,$2,$3) "
                "ON CONFLICT (subject_id,title) DO NOTHING RETURNING id",
                subject_id, title, created_by,
            )
            if list_id is None:
                return DUPLICATE_TOPIC_LIST
            await conn.executemany(
                "INSERT INTO subject_list_items(list_id,topic) VALUES($1,$2) ON CONFLICT DO NOTHING",
                [(list_id, topic) for topic in topics],
            )
            return list_id

    async def get_topic_list(self, list_id: int) -> dict | None:
        return await self._one("SELECT id,title,subject_id FROM subject_lists WHERE id=$1", list_id)

    async def add_topics(self, list_id: int, topics: list[str]) -> int | None:
        """Append topics to a list. Returns how many were new, TOPIC_LIST_FULL if over the cap,
        None if the list is gone."""
        async with self.transaction() as conn:
            # FOR UPDATE: two people extending the same list at once cannot exceed the cap together.
            if not await conn.fetchval("SELECT id FROM subject_lists WHERE id=$1 FOR UPDATE", list_id):
                return None
            existing = {
                r["topic"] for r in await conn.fetch("SELECT topic FROM subject_list_items WHERE list_id=$1", list_id)
            }
            new = [topic for topic in dict.fromkeys(topics) if topic not in existing]
            if len(existing) + len(new) > MAX_TOPICS_PER_LIST:
                return TOPIC_LIST_FULL
            await conn.executemany(
                "INSERT INTO subject_list_items(list_id,topic) VALUES($1,$2) ON CONFLICT DO NOTHING",
                [(list_id, topic) for topic in new],
            )
            return len(new)

    async def get_topic_lists(self, subject_id: int) -> list[dict]:
        lists = await self._all(
            "SELECT l.id, l.title, l.created_by, "
            "COALESCE(array_agg(i.topic ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL), '{}') AS topics "
            "FROM subject_lists l LEFT JOIN subject_list_items i ON i.list_id=l.id "
            "WHERE l.subject_id=$1 GROUP BY l.id ORDER BY l.id",
            subject_id,
        )
        for topic_list in lists:
            topic_list["topics"] = list(topic_list["topics"])
        return lists

    async def get_all_topic_lists(self) -> list[dict]:
        """Every topic list with its subject name, grouped by subject."""
        lists = await self._all(
            "SELECT l.id, l.title, s.name AS subject, "
            "COALESCE(array_agg(i.topic ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL), '{}') AS topics "
            "FROM subject_lists l JOIN subjects s ON s.id=l.subject_id "
            "LEFT JOIN subject_list_items i ON i.list_id=l.id "
            "GROUP BY l.id, s.name ORDER BY s.name, l.id"
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
