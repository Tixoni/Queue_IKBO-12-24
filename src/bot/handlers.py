import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.bot import keyboards, screens
from src.bot.formatting import Lesson, clean_text, pretty_date
from src.db import (
    DUPLICATE_TOPIC_LIST, JOINED, MAX_TOPICS_PER_LIST, TOPIC_LIST_FULL, QueueDB, in_booking_window,
)

log = logging.getLogger(__name__)
router = Router()

NAME_LENGTH = range(2, 61)
TOPIC_LIST_TITLE_LENGTH = range(2, 81)
MAX_TOPICS = 25
MAX_TOPIC_LENGTH = 80
MAX_TOPICS_TOTAL_LENGTH = 1800


class ProfileForm(StatesGroup):
    waiting_for_name = State()


class TopicListForm(StatesGroup):
    waiting_for_title = State()
    waiting_for_topics = State()
    waiting_for_more_topics = State()


def callback_arg(callback: CallbackQuery) -> str:
    """Last ':'-separated part of the callback data (an id or an ISO date)."""
    return callback.data.rsplit(":", 1)[1]


# --- commands and plain navigation ---------------------------------------------


@router.message(CommandStart())
async def start(message: Message, db: QueueDB) -> None:
    if message.from_user:
        await screens.answer_home(message, db, message.from_user)


@router.message(Command("queue"))
async def queue_command(message: Message, db: QueueDB) -> None:
    if not message.from_user:
        return
    rows = await db.get_user_registrations(message.from_user.id)
    if not rows:
        await message.answer(
            "У вас пока нет активных записей.",
            reply_markup=keyboards.menu_keyboard(screens.is_admin(message.from_user.id)),
        )
        return
    await message.answer(
        "👥 <b>Ваши записи</b> — выберите, чтобы увидеть очередь:",
        reply_markup=keyboards.queues_keyboard(rows),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.")


@router.callback_query(F.data == keyboards.HOME)
async def home(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_home(callback, db)


@router.callback_query(F.data == keyboards.SCHEDULE)
async def schedule_dates(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_schedule(callback, db)


@router.callback_query(F.data.startswith("ui:day:"))
async def schedule_day(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_schedule_day(callback, db, callback_arg(callback))


@router.callback_query(F.data.startswith("ui:lesson:"))
async def lesson(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_lesson(callback, db, int(callback_arg(callback)))


@router.callback_query(F.data == keyboards.MINE)
async def my_queues(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_my_queues(callback, db)


@router.callback_query(F.data.startswith("ui:deadline:"))
async def queue(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show_queue(callback, db, int(callback_arg(callback)))


# --- joining and leaving ---------------------------------------------------------


@router.callback_query(F.data.startswith("ui:join:"))
async def join(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    result = await db.join(deadline_id, callback.from_user.id, await screens.display_name(db, callback.from_user))
    await screens.safe_answer(callback, result, show_alert=result != JOINED)
    await screens.show_queue(callback, db, deadline_id, answer=False)


@router.callback_query(F.data.startswith("ui:leave:"))
async def leave(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    left = await db.leave(deadline_id, callback.from_user.id)
    await screens.safe_answer(callback, "Вы вышли из очереди." if left else "Вас уже нет в этой очереди.")
    await screens.show_queue(callback, db, deadline_id, answer=False)


# --- profile name ------------------------------------------------------------------


@router.callback_query(F.data == "ui:name")
async def ask_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileForm.waiting_for_name)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Напишите имя, которое будет показано в очереди. Чтобы отменить, отправьте /cancel."
        )


@router.message(ProfileForm.waiting_for_name, F.text)
async def save_name(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not message.from_user:
        return
    name = clean_text(message.text)
    if len(name) not in NAME_LENGTH:
        await message.answer("Имя должно быть от 2 до 60 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    await db.set_user_name(message.from_user.id, name)
    await state.clear()
    await message.answer(f"✅ Имя для очереди сохранено: <b>{escape(name)}</b>")
    await screens.answer_home(message, db, message.from_user)


# --- topic lists -----------------------------------------------------------------------


@router.callback_query(F.data.startswith("ui:topic-list:create:"))
async def start_topic_list(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    deadline = await db.get_deadline(deadline_id)
    if not deadline or not deadline.get("event_key"):
        await callback.answer("Не удалось определить пару из расписания.", show_alert=True)
        return
    lesson_row = await db.get_schedule_by_event_key(deadline["event_key"])
    if not lesson_row or not in_booking_window(date.fromisoformat(lesson_row["event_date"])):
        await callback.answer("Пара больше не найдена в расписании.", show_alert=True)
        return
    subject = Lesson.from_row(lesson_row).title
    await state.clear()
    await state.update_data(
        topic_deadline_id=deadline_id,
        topic_event_key=lesson_row["event_key"],
        topic_event_date=lesson_row["event_date"],
        topic_subject=subject,
    )
    await state.set_state(TopicListForm.waiting_for_title)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Создаём список для предмета «{escape(subject)}» на {pretty_date(lesson_row['event_date'])}.\n"
            "Введите название списка. Для отмены отправьте /cancel."
        )


@router.message(TopicListForm.waiting_for_title, F.text)
async def topic_list_title(message: Message, state: FSMContext) -> None:
    title = clean_text(message.text)
    if len(title) not in TOPIC_LIST_TITLE_LENGTH:
        await message.answer("Название списка должно быть от 2 до 80 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    await state.update_data(topic_list_title=title)
    await state.set_state(TopicListForm.waiting_for_topics)
    await message.answer("Теперь отправьте занятые темы одним сообщением, по одной теме на строке. Для отмены — /cancel.")


def parse_topics(text: str) -> list[str] | None:
    """Topics, one per line; None if empty, duplicated or over any limit."""
    topics = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    valid = (
        0 < len(topics) <= MAX_TOPICS
        and len(set(topics)) == len(topics)
        and all(len(topic) <= MAX_TOPIC_LENGTH for topic in topics)
        and sum(map(len, topics)) <= MAX_TOPICS_TOTAL_LENGTH
    )
    return topics if valid else None


@router.message(TopicListForm.waiting_for_topics, F.text)
async def topic_list_topics(message: Message, state: FSMContext, db: QueueDB) -> None:
    topics = parse_topics(message.text)
    if topics is None:
        await message.answer(
            "Укажите от 1 до 25 тем, каждая не длиннее 80 символов (до 1800 символов суммарно), по одной на строке."
        )
        return
    data = await state.get_data()
    list_id = await db.create_topic_list(
        deadline_id=data["topic_deadline_id"],
        event_key=data["topic_event_key"],
        event_date=data["topic_event_date"],
        subject=data["topic_subject"],
        title=data["topic_list_title"],
        topics=topics,
        created_by=message.from_user.id if message.from_user else 0,
    )
    if list_id is None:
        await state.clear()
        await message.answer("Не удалось создать список: очередь пары больше не найдена.")
    elif list_id == DUPLICATE_TOPIC_LIST:
        await state.set_state(TopicListForm.waiting_for_title)
        await message.answer("Для этой пары уже есть список с таким названием. Введите другое название или /cancel.")
    else:
        await state.clear()
        await message.answer(
            f"✅ Список «{escape(data['topic_list_title'])}» создан, тем: {len(topics)}.",
            reply_markup=keyboards.return_to_queue_keyboard(data["topic_deadline_id"]),
        )


@router.callback_query(F.data.startswith("ui:topic-list:add:"))
async def choose_topic_list(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    topic_lists = await db.get_topic_lists(deadline_id)
    if not topic_lists:
        await callback.answer("В этой очереди пока нет списков тем.", show_alert=True)
        return
    await screens.render(
        callback,
        "К какому списку добавить темы?",
        keyboards.pick_topic_list_keyboard(topic_lists, deadline_id),
    )


@router.callback_query(F.data.startswith("ui:topic-list:pick:"))
async def start_adding_topics(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    topic_list = await db.get_topic_list(int(callback_arg(callback)))
    if not topic_list:
        await callback.answer("Список больше не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(add_list_id=topic_list["id"], add_deadline_id=topic_list["deadline_id"])
    await state.set_state(TopicListForm.waiting_for_more_topics)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Добавляем темы в список «{escape(topic_list['title'])}».\n"
            "Отправьте новые темы одним сообщением, по одной на строке. Для отмены — /cancel."
        )


@router.message(TopicListForm.waiting_for_more_topics, F.text)
async def add_topics(message: Message, state: FSMContext, db: QueueDB) -> None:
    topics = parse_topics(message.text)
    if topics is None:
        await message.answer(
            "Укажите от 1 до 25 тем без повторов, каждая не длиннее 80 символов (до 1800 символов суммарно), "
            "по одной на строке. Или отправьте /cancel."
        )
        return
    data = await state.get_data()
    added = await db.add_topics(data["add_list_id"], topics)
    if added is None:
        await state.clear()
        await message.answer("Не удалось добавить темы: список или пара больше не найдены.")
        return
    if added == TOPIC_LIST_FULL:
        await message.answer(
            f"В списке может быть не больше {MAX_TOPICS_PER_LIST} тем. Отправьте меньше тем или /cancel."
        )
        return
    await state.clear()
    note = f"✅ Добавлено тем: {added}." if added else "Все эти темы уже есть в списке."
    if added < len(topics) and added:
        note += f" Уже были в списке: {len(topics) - added}."
    await message.answer(note, reply_markup=keyboards.return_to_queue_keyboard(data["add_deadline_id"]))


# --- administration ------------------------------------------------------------------------


@router.callback_query(F.data == "ui:admin")
async def admin_panel(callback: CallbackQuery) -> None:
    if not screens.is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await screens.render(
        callback,
        "⚙️ <b>Администрирование</b>\n\n"
        "Очереди на пары из расписания открываются автоматически.\n"
        "Список занятых тем может создать любой участник из карточки очереди.\n"
        "/admin_add_user ID_очереди TELEGRAM_ID Имя — вручную добавить студента",
        keyboards.back_keyboard(),
    )


@router.message(Command("admin_add_user"))
async def admin_add_user(message: Message, db: QueueDB) -> None:
    if not message.from_user or not screens.is_admin(message.from_user.id):
        await message.answer("Команда доступна администраторам.")
        return
    try:
        _, deadline_id, user_id, name = message.text.split(maxsplit=3)
        added = await db.add_user(int(deadline_id), int(user_id), clean_text(name)[:60])
    except ValueError:
        await message.answer("Формат: /admin_add_user ID_очереди TELEGRAM_ID Имя")
        return
    await message.answer("✅ Пользователь добавлен." if added else "Очередь не найдена или пользователь уже записан.")
