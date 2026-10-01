from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.formatting import Lesson, pretty_date

HOME = "ui:home"
SCHEDULE = "ui:schedule"
MINE = "ui:mine"


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nav(back: str, back_text: str = "↩️ Назад") -> list[list[InlineKeyboardButton]]:
    return [[_button(back_text, back)], [_button("🏠 Главное меню", HOME)]]


def menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [_button("📝 Записаться", SCHEDULE), _button("👥 Мои записи", MINE)],
        [_button("👤 Указать имя", "ui:name"), _button("🔄 Обновить расписание", "ui:refresh")],
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
    deadline_id: int, registered: bool, back: str, can_create_topic_list: bool, has_topic_lists: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    if can_create_topic_list:
        rows.append([_button("📌 Создать список тем", f"ui:topic-list:create:{deadline_id}")])
    if has_topic_lists:
        rows.append([_button("➕ Дополнить список тем", f"ui:topic-list:add:{deadline_id}")])
    rows.append([
        _button("🚪 Покинуть очередь", f"ui:leave:{deadline_id}") if registered
        else _button("✅ Записаться", f"ui:join:{deadline_id}")
    ])
    return _markup(rows + _nav(back))


def pick_topic_list_keyboard(topic_lists: list[dict], deadline_id: int) -> InlineKeyboardMarkup:
    buttons = [[_button(item["title"][:60], f"ui:topic-list:pick:{item['id']}")] for item in topic_lists]
    return _markup(buttons + _nav(f"ui:deadline:{deadline_id}", "↩️ К очереди"))


def return_to_queue_keyboard(deadline_id: int) -> InlineKeyboardMarkup:
    return _markup([[_button("↩️ Вернуться к очереди", f"ui:deadline:{deadline_id}")]])
