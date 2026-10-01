import ast
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from html import escape

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
DEFAULT_LESSON = "Занятие"


def pretty_date(value: str) -> str:
    day = date.fromisoformat(value)
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def clean_text(value: str) -> str:
    """Single-line user text: drops control and invisible formatting characters (incl. RTL overrides)."""
    value = "".join(" " if ch.isspace() else ch for ch in value if not unicodedata.category(ch).startswith("C") or ch.isspace())
    return " ".join(value.split())


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", value.casefold().replace("ё", "е"))


def _event_data(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Lesson:
    """Display-ready view of a schedule row."""

    title: str
    kind: str
    start: str
    end: str
    teachers: tuple[str, ...]
    room: tuple[str, ...]

    @classmethod
    def from_row(cls, row: dict) -> "Lesson":
        data = _event_data(row.get("raw"))
        teachers = tuple(
            str(item["name"]) for item in data.get("teachers") or []
            if isinstance(item, dict) and item.get("name")
        )
        room = data.get("room")
        room_parts = tuple(
            str(room[key]) for key in ("number", "campus") if room.get(key)
        ) if isinstance(room, dict) else ()
        return cls(
            title=str(data.get("discipline") or row.get("title") or DEFAULT_LESSON),
            kind=str(data.get("lesson_type") or DEFAULT_LESSON),
            start=str(data.get("time_start") or row.get("starts") or ""),
            end=str(data.get("time_end") or ""),
            teachers=teachers,
            room=room_parts,
        )

    def html(self, details: bool = True) -> str:
        time_text = ""
        if self.start:
            time_text = f" · {escape(self.start)}" + (f"–{escape(self.end)}" if self.end else "")
        lines = [f"<b>{escape(self.kind)} · {escape(self.title)}</b>{time_text}"]
        if details:
            if self.teachers:
                lines.append("Преподаватель: " + escape(", ".join(self.teachers)))
            if self.room:
                lines.append("Аудитория: " + escape(" · ".join(self.room)))
        return "\n".join(lines)

    def button_text(self) -> str:
        prefix = f"{self.start} · " if self.start else ""
        return f"{prefix}{self.kind}: {self.title}"[:64]


def lesson_matches_title(lesson: Lesson, title: str) -> bool:
    """Fuzzy link between a schedule lesson and a legacy queue created without an event key."""
    lesson_name = normalize_title(lesson.title)
    if len(lesson_name) < 4:
        return False
    queue_name = normalize_title(title)
    return lesson_name in queue_name or queue_name in lesson_name


def numbered(names: list[str]) -> str:
    return "\n".join(f"{number}. {escape(name)}" for number, name in enumerate(names, 1))


def bulleted(items: list[str]) -> str:
    return "\n".join(f"• {escape(item)}" for item in items)
