"""Screens: build the text and keyboard for a view and show it."""
import logging
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from src.bot import keyboards
from src.bot.formatting import Lesson, bulleted, clean_text, lesson_matches_title, numbered, pretty_date
from src.config import settings
from src.db import QueueDB

TOPIC_LISTS_TEXT_LIMIT = 2600
log = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


async def display_name(db: QueueDB, user: User) -> str:
    return await db.get_user_name(user.id) or clean_text(user.full_name)[:60] or "Без имени"


async def safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Answer a callback; ignore 'query is too old' errors (e.g. a button pressed while the bot was offline)."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        log.info("Callback answer skipped: %s", exc)


async def render(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup, answer: bool = True
) -> None:
    if answer:
        await safe_answer(callback)
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def home_text(db: QueueDB, user: User) -> str:
    text = (
        f"🎓 <b>Очередь на практические</b>\n\nПривет, {escape(await display_name(db, user))}!\n"
        "Выберите действие. Запись открыта на ближайшие 14 дней."
    )
    if is_admin(user.id):
        text += "\n\nВы администратор — настройки доступны по кнопке ниже."
    return text


async def answer_home(message: Message, db: QueueDB, user: User) -> None:
    await message.answer(await home_text(db, user), reply_markup=keyboards.menu_keyboard(is_admin(user.id)))


async def show_home(callback: CallbackQuery, db: QueueDB) -> None:
    await render(callback, await home_text(db, callback.from_user), keyboards.menu_keyboard(is_admin(callback.from_user.id)))


async def show_my_queues(callback: CallbackQuery, db: QueueDB) -> None:
    rows = await db.get_user_registrations(callback.from_user.id)
    if not rows:
        await render(
            callback,
            "У вас пока нет активных записей. Нажмите «Записаться», чтобы выбрать пару из расписания.",
            keyboards.back_keyboard(),
        )
        return
    await render(
        callback,
        "👥 <b>Ваши записи</b>\nВыберите практическую, чтобы посмотреть очередь:",
        keyboards.queues_keyboard(rows),
    )


async def show_schedule(callback: CallbackQuery, db: QueueDB, answer: bool = True) -> None:
    if await db.has_schedule():
        await render(
            callback,
            "📅 <b>Расписание</b>\nВыберите день недели. Верхний ряд — текущая неделя, нижний — следующая:",
            keyboards.week_days_keyboard(),
            answer=answer,
        )
    else:
        await render(
            callback,
            "📭 Расписание ещё не загружено. Нажмите «Обновить расписание» и попробуйте снова.",
            keyboards.back_keyboard(),
            answer=answer,
        )


async def show_schedule_day(callback: CallbackQuery, db: QueueDB, event_date: str) -> None:
    lessons = await db.list_schedule_for_date(event_date)
    if not lessons:
        await render(
            callback,
            f"На {pretty_date(event_date)} занятий в расписании нет.",
            keyboards.back_keyboard(keyboards.SCHEDULE),
        )
        return
    await render(
        callback,
        f"📅 <b>{pretty_date(event_date)}</b>\nВыберите пару, чтобы посмотреть детали и очередь:",
        keyboards.lessons_keyboard(lessons),
    )


async def show_lesson(callback: CallbackQuery, db: QueueDB, entry_id: int) -> None:
    lesson = await db.get_schedule_entry(entry_id)
    if not lesson:
        await render(callback, "Эта пара больше не найдена в расписании.", keyboards.back_keyboard(keyboards.SCHEDULE))
        return
    queue_id = await db.get_or_create_schedule_queue(
        lesson["event_key"], lesson["event_date"], Lesson.from_row(lesson).title
    )
    await show_queue(callback, db, queue_id, back=f"ui:day:{lesson['event_date']}", lesson=lesson)


def _topic_lists_text(topic_lists: list[dict]) -> str:
    parts = [f"<b>{escape(item['title'])}</b>\n{bulleted(item['topics'])}" for item in topic_lists]
    visible: list[str] = []
    for part in parts:
        if len("\n\n".join([*visible, part])) > TOPIC_LISTS_TEXT_LIMIT:
            break
        visible.append(part)
    text = "\n\n".join(visible)
    if len(visible) < len(parts):
        text += "\n\n… остальные списки скрыты"
    return text


async def _linked_lessons(db: QueueDB, deadline: dict, lesson: dict | None) -> list[dict]:
    if lesson:
        return [lesson]
    if deadline.get("event_key"):
        return []
    # Legacy queue without an event key: match by date and fuzzy title.
    rows = await db.list_schedule_for_date(deadline["event_date"])
    return [row for row in rows if lesson_matches_title(Lesson.from_row(row), deadline["title"])]


async def show_queue(
    callback: CallbackQuery,
    db: QueueDB,
    deadline_id: int,
    answer: bool = True,
    back: str = keyboards.MINE,
    lesson: dict | None = None,
) -> None:
    user_id = callback.from_user.id
    deadline = await db.get_deadline(deadline_id)
    if not deadline:
        await render(callback, "Эта очередь больше недоступна.", keyboards.back_keyboard(back))
        return

    if lesson is None and deadline.get("event_key"):
        lesson = await db.get_schedule_by_event_key(deadline["event_key"])
    if lesson and back == keyboards.MINE:
        back = f"ui:day:{lesson['event_date']}"

    linked = await _linked_lessons(db, deadline, lesson)
    queue = await db.get_queue(deadline_id)
    topic_lists = await db.get_topic_lists(deadline_id)
    topics = await db.get_topics(deadline_id)

    if linked:
        schedule_text = "📅 <b>Пара по расписанию</b>\n" + "\n\n".join(Lesson.from_row(row).html() for row in linked)
    else:
        schedule_text = "📅 Пара не найдена в загруженном расписании."

    text = (
        f"📘 <b>{escape(deadline['title'])}</b>\n"
        f"📅 {pretty_date(deadline['event_date'])}\n\n"
        f"{schedule_text}\n\n"
        f"👥 <b>Очередь · {len(queue)} чел.</b>\n{numbered(queue) or 'Пока никого нет — можно быть первым!'}"
    )
    if topic_lists:
        text += f"\n\n📌 <b>Списки занятых тем</b>\n{_topic_lists_text(topic_lists)}"
    if topics:
        text += f"\n\n📌 <b>Другие занятые темы</b>\n{bulleted(topics)}"
    if is_admin(user_id):
        text += f"\n\n🆔 ID очереди: <code>{deadline_id}</code>"

    keyboard = keyboards.queue_keyboard(
        deadline_id,
        registered=await db.is_registered(deadline_id, user_id),
        back=back,
        can_create_topic_list=lesson is not None,
        has_topic_lists=bool(topic_lists),
    )
    await render(callback, text, keyboard, answer=answer)
