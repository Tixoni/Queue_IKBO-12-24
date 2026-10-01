import json
import os
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# src.config reads the environment at import time; tests must never touch real secrets.
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "1"

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import User  # noqa: E402

from src.db import QueueDB  # noqa: E402

ADMIN_ID = 1
STUDENT_ID = 2


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def schedule_event(key: str, event_date: str, starts: str = "09:00", title: str = "Математика", **details) -> dict:
    raw = {"discipline": title, "lesson_type": "ПР", "time_start": starts, "time_end": "10:30", **details}
    return {"key": key, "date": event_date, "starts": starts, "title": title, "raw": json.dumps(raw, ensure_ascii=False)}


@pytest.fixture
async def db(tmp_path):
    database = QueueDB(tmp_path / "queue.sqlite3")
    await database.initialize()
    return database


@pytest.fixture
def user():
    return User(id=STUDENT_ID, is_bot=False, first_name="Иван", last_name="Петров")


@pytest.fixture
def admin():
    return User(id=ADMIN_ID, is_bot=False, first_name="Админ")


def make_callback(from_user: User, data: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=from_user,
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )


def make_message(from_user: User, text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, from_user=from_user, answer=AsyncMock())


def shown_text(callback: SimpleNamespace) -> str:
    return callback.message.edit_text.await_args.args[0]


def shown_buttons(callback: SimpleNamespace) -> list[tuple[str, str]]:
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    return [(button.text, button.callback_data) for row in markup.inline_keyboard for button in row]


@pytest.fixture
def state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=STUDENT_ID, user_id=STUDENT_ID))
