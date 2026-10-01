from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from src.bot.formatting import Lesson, pretty_date

HOME = "ui:home"
SCHEDULE = "ui:schedule"
MINE = "ui:mine"
TOPICS = "ui:topics"
NAME = "ui:name"

# Persistent bottom panel (reply keyboard). Its buttons send these texts as ordinary messages.
PANEL_SIGN_UP = "📝 Записаться"
PANEL_MINE = "👥 Мои записи"
PANEL_TOPICS = "📌 Темы"
PANEL_NAME = "👤 Имя"


def panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PANEL_SIGN_UP), KeyboardButton(text=PANEL_MINE)],
            [KeyboardButton(text=PANEL_TOPICS), KeyboardButton(text=PANEL_NAME)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nav(back: str, back_text: str = "↩️ Назад") -> list[list[InlineKeyboardButton]]:
    return [[_button(back_text, back)], [_button("🏠 Главное меню", HOME)]]


def menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [_button(PANEL_SIGN_UP, SCHEDULE), _button(PANEL_MINE, MINE)],
        [_button(PANEL_TOPICS, TOPICS), _button("👤 Указать имя", NAME)],
    ]
    if is_admin:
        rows.append([_button("⚙️ Администрирование", "ui:admin")])
    return _markup(rows)


def back_keyboard(back: str = HOME) -> InlineKeyboardMarkup:
    return _markup(_nav(back))


def queues_keyboard(rows: list[dict], back: str = HOME) -> InlineKeyboardMarkup:
    buttons = [
        [_button(f"{pretty_date(row['date'])} · {row['title'][:32]}", f"ui:deadline:{row['id']}")]
        for row in rows
    ]
    return _markup(buttons + _nav(back))


def week_days_keyboard() -> InlineKeyboardMarkup:
    """Two rows of Mon–Sat buttons: the current week and the next one."""
    monday = date.today() - timedelta(days=date.today().weekday())
    labels = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ")
    buttons = [
        [
            _button(label, f"ui:day:{(monday + timedelta(days=week * 7 + offset)).isoformat()}")
            for offset, label in enumerate(labels)
        ]
        for week in (0, 1)
    ]
    return _markup(buttons + _nav(HOME))


def lessons_keyboard(rows: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [_button(Lesson.from_row(row).button_text(), f"ui:lesson:{row['id']}")] for row in rows
    ]
    return _markup(buttons + _nav(SCHEDULE, "↩️ К выбору даты"))


def queue_keyboard(
    deadline_id: int, registered: bool, back: str, subject_id: int | None = None, is_admin: bool = False
) -> InlineKeyboardMarkup:
    rows = [[
        _button("🚪 Покинуть очередь", f"ui:leave:{deadline_id}") if registered
        else _button("✅ Записаться", f"ui:join:{deadline_id}")
    ]]
    if subject_id is not None:
        rows.append([_button("📌 Темы по предмету", f"ui:subj:{subject_id}")])
    if is_admin:
        rows.append([_button("✏️ Изменить название", f"ui:rename:{deadline_id}")])
    return _markup(rows + _nav(back))


def subjects_keyboard(subjects: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [_button(f"{item['name'][:56]}" + (f" ({item['lists']})" if item["lists"] else ""), f"ui:subj:{item['id']}")]
        for item in subjects
    ]
    return _markup(buttons + [[_button("🏠 Главное меню", HOME)]])


def subject_keyboard(subject_id: int, topic_lists: list[dict]) -> InlineKeyboardMarkup:
    rows = [[_button("➕ Новый список", f"ui:tl:new:{subject_id}")]]
    rows += [[_button(f"✏️ Дополнить: {item['title'][:44]}", f"ui:tl:add:{item['id']}")] for item in topic_lists]
    return _markup(rows + _nav(TOPICS, "↩️ К предметам"))


def to_subject_keyboard(subject_id: int) -> InlineKeyboardMarkup:
    return _markup([[_button("↩️ К спискам тем", f"ui:subj:{subject_id}")]])


def to_queue_keyboard(deadline_id: int) -> InlineKeyboardMarkup:
    return _markup([[_button("↩️ Вернуться к очереди", f"ui:deadline:{deadline_id}")]])
