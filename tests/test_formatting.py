from datetime import date

import pytest

from src.bot import keyboards
from src.bot.formatting import Lesson, clean_text, lesson_matches_title, numbered, pretty_date

from tests.conftest import schedule_event


def test_pretty_date():
    assert pretty_date("2026-10-05") == "понедельник, 5 октября"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Иван   Петров ", "Иван Петров"),
        ("Иван\nПетров\t", "Иван Петров"),
        ("а‮б​в\x00г", "абвг"),  # RTL override, zero-width space, NUL
        ("<b>x</b>", "<b>x</b>"),  # HTML is escaped on output, not stripped
        ("​​", ""),
    ],
)
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def test_lesson_from_json_row():
    row = schedule_event("k", "2026-10-05", teachers=[{"name": "Иванов И.И."}], room={"number": "А-1", "campus": "В-78"})
    lesson = Lesson.from_row(row)

    assert (lesson.title, lesson.kind, lesson.start, lesson.end) == ("Математика", "ПР", "09:00", "10:30")
    assert lesson.teachers == ("Иванов И.И.",)
    assert lesson.room == ("А-1", "В-78")
    assert lesson.html() == (
        "<b>ПР · Математика</b> · 09:00–10:30\nПреподаватель: Иванов И.И.\nАудитория: А-1 · В-78"
    )
    assert lesson.button_text() == "09:00 · ПР: Математика"


def test_lesson_from_legacy_python_repr_and_broken_raw():
    legacy = {"title": "X", "starts": "10:00", "raw": str({"discipline": "Физика", "lesson_type": "ЛК"})}
    broken = {"title": "Химия", "starts": "", "raw": "{not json"}

    assert Lesson.from_row(legacy).title == "Физика"
    assert Lesson.from_row(legacy).start == "10:00"
    assert Lesson.from_row(broken).title == "Химия"
    assert Lesson.from_row(broken).kind == "Занятие"
    assert Lesson.from_row(broken).html(details=False) == "<b>Занятие · Химия</b>"


def test_lesson_html_escapes_schedule_data():
    row = schedule_event("k", "2026-10-05", title="<script>")
    assert "&lt;script&gt;" in Lesson.from_row(row).html()


def test_button_text_is_limited_to_telegram_size():
    row = schedule_event("k", "2026-10-05", title="Очень длинное название дисциплины " * 5)
    assert len(Lesson.from_row(row).button_text()) == 64


def test_lesson_matches_title():
    lesson = Lesson.from_row(schedule_event("k", "2026-10-05", title="Моделирование бизнес-процессов"))

    assert lesson_matches_title(lesson, "ПР Моделирование бизнес процессов")
    assert not lesson_matches_title(lesson, "Физика")
    assert not lesson_matches_title(Lesson.from_row(schedule_event("k", "2026-10-05", title="ИИ")), "ИИ")


def test_numbered_escapes_names():
    assert numbered(["<b>Вася</b>", "Петя"]) == "1. &lt;b&gt;Вася&lt;/b&gt;\n2. Петя"


def test_week_days_keyboard_covers_two_weeks_from_monday():
    rows = keyboards.week_days_keyboard().inline_keyboard
    dates = [date.fromisoformat(button.callback_data.split(":")[-1]) for row in rows[:2] for button in row]

    assert len(dates) == 12
    assert dates[0].weekday() == 0
    assert 0 <= (date.today() - dates[0]).days < 7
    assert (dates[6] - dates[0]).days == 7
    assert all(len(button.callback_data.encode()) <= 64 for row in rows for button in row)


def test_menu_keyboard_has_no_refresh_button():
    data = [b.callback_data for row in keyboards.menu_keyboard(is_admin=True).inline_keyboard for b in row]

    assert "ui:refresh" not in data
    assert "ui:admin" in data
    assert "ui:admin" not in [b.callback_data for row in keyboards.menu_keyboard(False).inline_keyboard for b in row]
