"""Screens: each builder returns the text and inline keyboard of one view.

`show` puts a screen into the message an inline button belongs to; `send` posts it as a new message
(used by the bottom panel, whose buttons arrive as plain text messages).
"""
import logging
from dataclasses import dataclass
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from src.bot import keyboards
from src.bot.formatting import Lesson, bulleted, clean_text, lesson_matches_title, numbered, pretty_date
from src.config import settings
from src.db import QueueDB

TOPIC_LISTS_TEXT_LIMIT = 3300
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


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


async def show(callback: CallbackQuery, screen: Screen, answer: bool = True) -> None:
    if answer:
        await safe_answer(callback)
    if not callback.message:
        return
    try:
        await callback.message.edit_text(screen.text, reply_markup=screen.keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def send(message: Message, screen: Screen) -> None:
    await message.answer(screen.text, reply_markup=screen.keyboard)


# --- navigation --------------------------------------------------------------------


async def home(db: QueueDB, user: User) -> Screen:
    text = (
        f"🎓 <b>Очередь на практические</b>\n\nПривет, {escape(await display_name(db, user))}!\n"
        "Выберите действие. Запись открыта на ближайшие 14 дней."
    )
    if is_admin(user.id):
        text += "\n\nВы администратор — настройки доступны по кнопке ниже."
    return Screen(text, keyboards.menu_keyboard(is_admin(user.id)))


async def my_queues(db: QueueDB, user: User) -> Screen:
    rows = await db.get_user_registrations(user.id)
    if not rows:
        return Screen(
            "У вас пока нет активных записей. Нажмите «Записаться», чтобы выбрать пару из расписания.",
            keyboards.back_keyboard(),
        )
    return Screen("👥 <b>Ваши записи</b>\nВыберите практическую, чтобы посмотреть очередь:",
                  keyboards.queues_keyboard(rows))


async def schedule(db: QueueDB) -> Screen:
    if not await db.has_schedule():
        return Screen(
            "📭 Расписание ещё не загружено — бот подтянет его автоматически. Загляните сюда чуть позже.",
            keyboards.back_keyboard(),
        )
    return Screen(
        "📅 <b>Расписание</b>\nВыберите день недели. Верхний ряд — текущая неделя, нижний — следующая:",
        keyboards.week_days_keyboard(),
    )


async def schedule_day(db: QueueDB, event_date: str) -> Screen:
    lessons = await db.list_schedule_for_date(event_date)
    if not lessons:
        return Screen(f"На {pretty_date(event_date)} занятий в расписании нет.",
                      keyboards.back_keyboard(keyboards.SCHEDULE))
    return Screen(f"📅 <b>{pretty_date(event_date)}</b>\nВыберите пару, чтобы посмотреть детали и очередь:",
                  keyboards.lessons_keyboard(lessons))


# --- queues ----------------------------------------------------------------------------


async def lesson(db: QueueDB, user: User, entry_id: int) -> Screen:
    row = await db.get_schedule_entry(entry_id)
    if not row:
        return Screen("Эта пара больше не найдена в расписании.", keyboards.back_keyboard(keyboards.SCHEDULE))
    queue_id = await db.get_or_create_schedule_queue(row["event_key"], row["event_date"], Lesson.from_row(row).title)
    return await queue(db, user, queue_id, back=f"ui:day:{row['event_date']}", lesson_row=row)


async def _linked_lessons(db: QueueDB, deadline: dict, lesson_row: dict | None) -> list[dict]:
    if lesson_row:
        return [lesson_row]
    if deadline.get("event_key"):
        return []
    # Legacy queue without an event key: match by date and fuzzy title.
    rows = await db.list_schedule_for_date(deadline["event_date"])
    return [row for row in rows if lesson_matches_title(Lesson.from_row(row), deadline["title"])]


async def queue(
    db: QueueDB, user: User, deadline_id: int, back: str = keyboards.MINE, lesson_row: dict | None = None
) -> Screen:
    deadline = await db.get_deadline(deadline_id)
    if not deadline:
        return Screen("Эта очередь больше недоступна.", keyboards.back_keyboard(back))

    if lesson_row is None and deadline.get("event_key"):
        lesson_row = await db.get_schedule_by_event_key(deadline["event_key"])
    if lesson_row and back == keyboards.MINE:
        back = f"ui:day:{lesson_row['event_date']}"

    linked = await _linked_lessons(db, deadline, lesson_row)
    names = await db.get_queue(deadline_id)
    if linked:
        schedule_text = "📅 <b>Пара по расписанию</b>\n" + "\n\n".join(Lesson.from_row(row).html() for row in linked)
    else:
        schedule_text = "📅 Пара не найдена в загруженном расписании."

    text = (
        f"📘 <b>{escape(deadline['title'])}</b>\n"
        f"📅 {pretty_date(deadline['event_date'])}\n\n"
        f"{schedule_text}\n\n"
        f"👥 <b>Очередь · {len(names)} чел.</b>\n{numbered(names) or 'Пока никого нет — можно быть первым!'}"
    )
    if is_admin(user.id):
        text += f"\n\n🆔 ID очереди: <code>{deadline_id}</code>"

    subject_id = await db.ensure_subject(Lesson.from_row(linked[0]).title) if linked else None
    return Screen(text, keyboards.queue_keyboard(
        deadline_id,
        registered=await db.is_registered(deadline_id, user.id),
        back=back,
        subject_id=subject_id,
        is_admin=is_admin(user.id),
    ))


# --- topic lists ---------------------------------------------------------------------------


async def subjects(db: QueueDB) -> Screen:
    current = sorted({Lesson.from_row(row).title for row in await db.list_window_schedule()})
    items = await db.list_subjects(current)
    if not items:
        return Screen("📌 Предметов пока нет: расписание ещё не загружено.", keyboards.back_keyboard())
    return Screen(
        "📌 <b>Списки тем</b>\nВыберите предмет. В скобках — сколько у него списков.",
        keyboards.subjects_keyboard(items),
    )


def _topic_lists_text(topic_lists: list[dict]) -> str:
    parts = [f"<b>{escape(item['title'])}</b>\n{bulleted(item['topics']) or '—'}" for item in topic_lists]
    visible: list[str] = []
    for part in parts:
        if len("\n\n".join([*visible, part])) > TOPIC_LISTS_TEXT_LIMIT:
            break
        visible.append(part)
    text = "\n\n".join(visible)
    if len(visible) < len(parts):
        text += f"\n\n… и ещё списков: {len(parts) - len(visible)}"
    return text


async def subject(db: QueueDB, subject_id: int) -> Screen:
    item = await db.get_subject(subject_id)
    if not item:
        return Screen("Предмет не найден.", keyboards.back_keyboard(keyboards.TOPICS))
    topic_lists = await db.get_topic_lists(subject_id)
    body = _topic_lists_text(topic_lists) if topic_lists else "Списков пока нет — создайте первый."
    return Screen(f"📌 <b>{escape(item['name'])}</b>\n\n{body}", keyboards.subject_keyboard(subject_id, topic_lists))


MESSAGE_LIMIT = 3800  # Telegram allows 4096 characters per message; keep a margin for markup


def _split_blocks(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Pack blocks into as few messages as possible; an oversized block is split by lines."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        pieces = [block]
        if len(block) > limit:
            pieces, piece = [], ""
            for line in block.split("\n"):
                if piece and len(piece) + len(line) + 1 > limit:
                    pieces.append(piece)
                    piece = ""
                piece = f"{piece}\n{line}" if piece else line
            pieces.append(piece)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                messages.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        messages.append(current)
    return messages


async def all_topic_lists(db: QueueDB) -> list[Screen]:
    """All lists of all subjects; may take several messages, the last one carries the navigation."""
    topic_lists = await db.get_all_topic_lists()
    if not topic_lists:
        return [Screen("📋 Списков тем пока нет. Создайте первый в разделе нужного предмета.",
                       keyboards.after_all_topics_keyboard())]
    by_subject: dict[str, list[str]] = {}
    for item in topic_lists:
        by_subject.setdefault(item["subject"], []).append(
            f"<b>{escape(item['title'])}</b>\n{bulleted(item['topics']) or '—'}"
        )
    blocks = [f"📌 <u>{escape(subject)}</u>\n\n" + "\n\n".join(parts) for subject, parts in by_subject.items()]
    messages = _split_blocks([f"📋 <b>Все списки тем</b> · {len(topic_lists)}"] + blocks)
    return [Screen(text) for text in messages[:-1]] + [Screen(messages[-1], keyboards.after_all_topics_keyboard())]


# --- administration ---------------------------------------------------------------------------


def admin_panel() -> Screen:
    return Screen(
        "⚙️ <b>Администрирование</b>\n\n"
        "Очереди на пары из расписания открываются автоматически.\n"
        "Чтобы дописать условие к очереди, откройте её и нажмите «✏️ Изменить название».\n"
        "Списки тем ведутся в разделе «📌 Темы» по предметам, их может создавать любой участник.\n"
        "/admin_add_user ID_очереди TELEGRAM_ID Имя — вручную добавить студента",
        keyboards.back_keyboard(),
    )
