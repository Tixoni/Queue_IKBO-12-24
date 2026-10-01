import asyncio
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import aiosqlite

BOOKING_DAYS = 14
JOINED = "Вы добавлены в очередь."
DUPLICATE_TOPIC_LIST = -1
TOPIC_LIST_FULL = -1
MAX_TOPICS_PER_LIST = 60

SCHEDULE_COLUMNS = "rowid AS id,event_key,event_date,starts,title,raw"


def booking_window() -> tuple[date, date]:
    today = date.today()
    return today, today + timedelta(days=BOOKING_DAYS)


def in_booking_window(day: date) -> bool:
    start, end = booking_window()
    return start <= day <= end


class QueueDB:
    """Single-PC storage. SQLite serializes writes; BEGIN IMMEDIATE makes seat/queue decisions atomic."""

    def __init__(self, path: Path):
        self.path = path
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=30000")
            yield db

    @asynccontextmanager
    async def transaction(self):
        """Exclusive write transaction: commits on normal exit, rolls back on error."""
        async with self._write_lock, self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            await db.commit()

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.connect() as db:
            cur = await db.execute(sql, params)
            return [dict(row) for row in await cur.fetchall()]

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.connect() as db:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def initialize(self):
        async with self.connect() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS schedule (
                    event_key TEXT PRIMARY KEY, event_date TEXT NOT NULL, starts TEXT,
                    title TEXT NOT NULL, raw TEXT
                );
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_date TEXT NOT NULL,
                    title TEXT NOT NULL, capacity INTEGER, active INTEGER NOT NULL DEFAULT 1,
                    event_key TEXT
                );
                CREATE TABLE IF NOT EXISTS registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, deadline_id INTEGER NOT NULL REFERENCES deadlines(id),
                    user_id INTEGER NOT NULL, display_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(deadline_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS occupied_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, deadline_id INTEGER NOT NULL REFERENCES deadlines(id),
                    topic TEXT NOT NULL, UNIQUE(deadline_id, topic)
                );
                CREATE TABLE IF NOT EXISTS topic_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deadline_id INTEGER NOT NULL REFERENCES deadlines(id),
                    event_key TEXT NOT NULL, event_date TEXT NOT NULL, subject TEXT NOT NULL,
                    title TEXT NOT NULL, created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(event_key, title)
                );
                CREATE TABLE IF NOT EXISTS topic_list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_list_id INTEGER NOT NULL REFERENCES topic_lists(id),
                    topic TEXT NOT NULL, UNIQUE(topic_list_id, topic)
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL
                );
            """)
            columns = await (await db.execute("PRAGMA table_info(deadlines)")).fetchall()
            if "event_key" not in {row[1] for row in columns}:
                await db.execute("ALTER TABLE deadlines ADD COLUMN event_key TEXT")
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_deadlines_event_key "
                "ON deadlines(event_key) WHERE event_key IS NOT NULL"
            )

    # --- schedule -----------------------------------------------------------

    async def sync_schedule(self, events: list[dict]):
        start, end = booking_window()
        async with self.transaction() as db:
            await db.execute(
                "DELETE FROM schedule WHERE event_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
            await db.executemany(
                "INSERT OR REPLACE INTO schedule(event_key,event_date,starts,title,raw) VALUES(?,?,?,?,?)",
                [(e["key"], e["date"], e.get("starts"), e["title"], e.get("raw")) for e in events],
            )

    async def has_schedule(self) -> bool:
        start, end = booking_window()
        row = await self._one(
            "SELECT 1 AS ok FROM schedule WHERE event_date BETWEEN ? AND ? LIMIT 1",
            (start.isoformat(), end.isoformat()),
        )
        return row is not None

    async def list_schedule_for_date(self, event_date: str) -> list[dict]:
        return await self._all(
            f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE event_date=? ORDER BY starts,title",
            (event_date,),
        )

    async def get_schedule_entry(self, entry_id: int) -> dict | None:
        return await self._one(f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE rowid=?", (entry_id,))

    async def get_schedule_by_event_key(self, event_key: str) -> dict | None:
        return await self._one(f"SELECT {SCHEDULE_COLUMNS} FROM schedule WHERE event_key=?", (event_key,))

    # --- queues -------------------------------------------------------------

    async def get_deadline(self, deadline_id: int) -> dict | None:
        return await self._one(
            "SELECT id,event_date,title,event_key FROM deadlines WHERE id=? AND active=1", (deadline_id,)
        )

    async def get_or_create_schedule_queue(self, event_key: str, event_date: str, title: str) -> int:
        """Return the persistent queue ID for a schedule event, creating it on first use."""
        async with self.transaction() as db:
            cur = await db.execute("SELECT id FROM deadlines WHERE event_key=?", (event_key,))
            row = await cur.fetchone()
            if row:
                return row["id"]
            cur = await db.execute(
                "INSERT INTO deadlines(event_date,title,event_key) VALUES(?,?,?)",
                (event_date, title, event_key),
            )
            return cur.lastrowid

    async def join(self, deadline_id: int, user_id: int, name: str) -> str:
        async with self.transaction() as db:
            cur = await db.execute("SELECT event_date,active FROM deadlines WHERE id=?", (deadline_id,))
            deadline = await cur.fetchone()
            if not deadline or not deadline["active"]:
                return "Очередь не найдена или запись закрыта."
            if not in_booking_window(date.fromisoformat(deadline["event_date"])):
                return "Запись доступна только на пары в ближайшие 2 недели."
            cur = await db.execute(
                "INSERT OR IGNORE INTO registrations(deadline_id,user_id,display_name) VALUES(?,?,?)",
                (deadline_id, user_id, name),
            )
            return JOINED if cur.rowcount == 1 else "Вы уже записаны."

    async def leave(self, deadline_id: int, user_id: int) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                "DELETE FROM registrations WHERE deadline_id=? AND user_id=?", (deadline_id, user_id)
            )
            return cur.rowcount > 0

    async def add_user(self, deadline_id: int, user_id: int, name: str) -> bool:
        async with self.transaction() as db:
            cur = await db.execute("SELECT 1 FROM deadlines WHERE id=? AND active=1", (deadline_id,))
            if not await cur.fetchone():
                return False
            cur = await db.execute(
                "INSERT OR IGNORE INTO registrations(deadline_id,user_id,display_name) VALUES(?,?,?)",
                (deadline_id, user_id, name),
            )
            return cur.rowcount == 1

    async def get_queue(self, deadline_id: int) -> list[str]:
        rows = await self._all(
            "SELECT display_name FROM registrations WHERE deadline_id=? ORDER BY id", (deadline_id,)
        )
        return [row["display_name"] for row in rows]

    async def is_registered(self, deadline_id: int, user_id: int) -> bool:
        row = await self._one(
            "SELECT 1 AS ok FROM registrations WHERE deadline_id=? AND user_id=?", (deadline_id, user_id)
        )
        return row is not None

    async def get_user_registrations(self, user_id: int) -> list[dict]:
        return await self._all(
            "SELECT d.id,d.event_date AS date,d.title FROM registrations r "
            "JOIN deadlines d ON d.id=r.deadline_id "
            "WHERE r.user_id=? AND d.active=1 ORDER BY d.event_date,d.id",
            (user_id,),
        )

    # --- topics -------------------------------------------------------------

    async def get_topics(self, deadline_id: int) -> list[str]:
        rows = await self._all(
            "SELECT topic FROM occupied_topics WHERE deadline_id=? ORDER BY topic", (deadline_id,)
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
        async with self.transaction() as db:
            cur = await db.execute(
                "SELECT event_key,event_date FROM deadlines WHERE id=? AND active=1", (deadline_id,)
            )
            queue = await cur.fetchone()
            if not queue or queue["event_key"] != event_key or queue["event_date"] != event_date:
                return None
            if not in_booking_window(date.fromisoformat(event_date)):
                return None
            cur = await db.execute(
                "SELECT 1 FROM topic_lists WHERE event_key=? AND title=?", (event_key, title)
            )
            if await cur.fetchone():
                return DUPLICATE_TOPIC_LIST
            cur = await db.execute(
                "INSERT INTO topic_lists(deadline_id,event_key,event_date,subject,title,created_by) "
                "VALUES(?,?,?,?,?,?)",
                (deadline_id, event_key, event_date, subject, title, created_by),
            )
            list_id = cur.lastrowid
            await db.executemany(
                "INSERT OR IGNORE INTO topic_list_items(topic_list_id,topic) VALUES(?,?)",
                [(list_id, topic) for topic in topics],
            )
            return list_id

    async def get_topic_list(self, topic_list_id: int) -> dict | None:
        return await self._one(
            "SELECT l.id,l.title,l.deadline_id FROM topic_lists l "
            "JOIN deadlines d ON d.id=l.deadline_id WHERE l.id=? AND d.active=1",
            (topic_list_id,),
        )

    async def add_topics(self, topic_list_id: int, topics: list[str]) -> int | None:
        """Append topics to a list. Returns how many were new, TOPIC_LIST_FULL if over the cap,
        None if the list or its pair is gone."""
        async with self.transaction() as db:
            cur = await db.execute(
                "SELECT l.event_date FROM topic_lists l JOIN deadlines d ON d.id=l.deadline_id "
                "WHERE l.id=? AND d.active=1",
                (topic_list_id,),
            )
            row = await cur.fetchone()
            if not row or not in_booking_window(date.fromisoformat(row["event_date"])):
                return None
            cur = await db.execute("SELECT topic FROM topic_list_items WHERE topic_list_id=?", (topic_list_id,))
            existing = {r["topic"] for r in await cur.fetchall()}
            new = [topic for topic in topics if topic not in existing]
            if len(existing) + len(new) > MAX_TOPICS_PER_LIST:
                return TOPIC_LIST_FULL
            await db.executemany(
                "INSERT OR IGNORE INTO topic_list_items(topic_list_id,topic) VALUES(?,?)",
                [(topic_list_id, topic) for topic in new],
            )
            return len(new)

    async def get_topic_lists(self, deadline_id: int) -> list[dict]:
        lists = await self._all(
            "SELECT id,title,subject,event_date,created_by FROM topic_lists WHERE deadline_id=? ORDER BY id",
            (deadline_id,),
        )
        for topic_list in lists:
            rows = await self._all(
                "SELECT topic FROM topic_list_items WHERE topic_list_id=? ORDER BY id", (topic_list["id"],)
            )
            topic_list["topics"] = [row["topic"] for row in rows]
        return lists

    # --- profiles -----------------------------------------------------------

    async def get_user_name(self, user_id: int) -> str | None:
        row = await self._one("SELECT display_name FROM profiles WHERE user_id=?", (user_id,))
        return row["display_name"] if row else None

    async def set_user_name(self, user_id: int, name: str):
        async with self.transaction() as db:
            await db.execute(
                "INSERT INTO profiles(user_id,display_name) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name",
                (user_id, name),
            )
