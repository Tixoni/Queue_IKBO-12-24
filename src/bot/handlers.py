import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.bot import keyboards, screens
from src.bot.formatting import clean_text
from src.db import DUPLICATE_TOPIC_LIST, JOINED, MAX_TOPICS_PER_LIST, TOPIC_LIST_FULL, QueueDB

log = logging.getLogger(__name__)
router = Router()

NAME_LENGTH = range(2, 61)
QUEUE_TITLE_LENGTH = range(2, 201)
TOPIC_LIST_TITLE_LENGTH = range(2, 81)
MAX_TOPICS = 25
MAX_TOPIC_LENGTH = 80
MAX_TOPICS_TOTAL_LENGTH = 1800

TOPICS_FORMAT_HINT = (
    "Укажите от 1 до 25 тем без повторов, каждая не длиннее 80 символов (до 1800 символов суммарно), "
    "по одной на строке. Или отправьте /cancel."
)


class ProfileForm(StatesGroup):
    waiting_for_name = State()


class TopicListForm(StatesGroup):
    waiting_for_title = State()
    waiting_for_topics = State()
    waiting_for_more_topics = State()


class RenameForm(StatesGroup):
    waiting_for_title = State()


def callback_arg(callback: CallbackQuery) -> str:
    """Last ':'-separated part of the callback data (an id or an ISO date)."""
    return callback.data.rsplit(":", 1)[1]


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


async def ask_for_name(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileForm.waiting_for_name)
    await message.answer("Напишите имя, которое будет показано в очереди. Чтобы отменить, отправьте /cancel.")


# --- bottom panel: registered first, so it works even in the middle of a dialog ----------------


@router.message(F.text == keyboards.PANEL_SIGN_UP)
async def panel_sign_up(message: Message, state: FSMContext, db: QueueDB) -> None:
    await state.clear()
    await screens.send(message, await screens.schedule(db))


@router.message(F.text == keyboards.PANEL_MINE)
async def panel_mine(message: Message, state: FSMContext, db: QueueDB) -> None:
    await state.clear()
    await screens.send(message, await screens.my_queues(db, message.from_user))


@router.message(F.text == keyboards.PANEL_TOPICS)
async def panel_topics(message: Message, state: FSMContext, db: QueueDB) -> None:
    await state.clear()
    await screens.send(message, await screens.subjects(db))


@router.message(F.text == keyboards.PANEL_NAME)
async def panel_name(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ask_for_name(message, state)


# --- commands and navigation ---------------------------------------------------------------------


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not message.from_user:
        return
    await state.clear()
    await message.answer("Кнопки быстрого доступа — внизу экрана 👇", reply_markup=keyboards.panel_keyboard())
    await screens.send(message, await screens.home(db, message.from_user))


@router.message(Command("queue"))
async def queue_command(message: Message, db: QueueDB) -> None:
    if message.from_user:
        await screens.send(message, await screens.my_queues(db, message.from_user))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=keyboards.panel_keyboard())


@router.callback_query(F.data == keyboards.HOME)
async def home(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.home(db, callback.from_user))


@router.callback_query(F.data == keyboards.SCHEDULE)
async def schedule_dates(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.schedule(db))


@router.callback_query(F.data.startswith("ui:day:"))
async def schedule_day(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.schedule_day(db, callback_arg(callback)))


@router.callback_query(F.data.startswith("ui:lesson:"))
async def lesson(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.lesson(db, callback.from_user, int(callback_arg(callback))))


@router.callback_query(F.data == keyboards.MINE)
async def my_queues(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.my_queues(db, callback.from_user))


@router.callback_query(F.data.startswith("ui:deadline:"))
async def queue(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.queue(db, callback.from_user, int(callback_arg(callback))))


# --- joining and leaving -----------------------------------------------------------------------


@router.callback_query(F.data.startswith("ui:join:"))
async def join(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    result = await db.join(deadline_id, callback.from_user.id, await screens.display_name(db, callback.from_user))
    await screens.safe_answer(callback, result, show_alert=result != JOINED)
    await screens.show(callback, await screens.queue(db, callback.from_user, deadline_id), answer=False)


@router.callback_query(F.data.startswith("ui:leave:"))
async def leave(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    left = await db.leave(deadline_id, callback.from_user.id)
    await screens.safe_answer(callback, "Вы вышли из очереди." if left else "Вас уже нет в этой очереди.")
    await screens.show(callback, await screens.queue(db, callback.from_user, deadline_id), answer=False)


# --- profile name ------------------------------------------------------------------------------


@router.callback_query(F.data == keyboards.NAME)
async def name_button(callback: CallbackQuery, state: FSMContext) -> None:
    await screens.safe_answer(callback)
    if callback.message:
        await ask_for_name(callback.message, state)


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
    await screens.send(message, await screens.home(db, message.from_user))


# --- topic lists by subject ----------------------------------------------------------------------


@router.callback_query(F.data == keyboards.TOPICS)
async def subjects(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.subjects(db))


@router.callback_query(F.data.startswith("ui:subj:"))
async def subject(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.subject(db, int(callback_arg(callback))))


@router.callback_query(F.data.startswith("ui:tl:new:"))
async def start_topic_list(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    item = await db.get_subject(int(callback_arg(callback)))
    if not item:
        await screens.safe_answer(callback, "Предмет не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(subject_id=item["id"])
    await state.set_state(TopicListForm.waiting_for_title)
    await screens.safe_answer(callback)
    if callback.message:
        await callback.message.answer(
            f"Новый список для предмета «{escape(item['name'])}».\n"
            "Введите название списка (например, «Темы докладов»). Для отмены отправьте /cancel."
        )


@router.message(TopicListForm.waiting_for_title, F.text)
async def topic_list_title(message: Message, state: FSMContext) -> None:
    title = clean_text(message.text)
    if len(title) not in TOPIC_LIST_TITLE_LENGTH:
        await message.answer("Название списка должно быть от 2 до 80 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    await state.update_data(list_title=title)
    await state.set_state(TopicListForm.waiting_for_topics)
    await message.answer("Теперь отправьте занятые темы одним сообщением, по одной теме на строке. Для отмены — /cancel.")


@router.message(TopicListForm.waiting_for_topics, F.text)
async def topic_list_topics(message: Message, state: FSMContext, db: QueueDB) -> None:
    topics = parse_topics(message.text)
    if topics is None:
        await message.answer(TOPICS_FORMAT_HINT)
        return
    data = await state.get_data()
    list_id = await db.create_topic_list(data["subject_id"], data["list_title"], topics, message.from_user.id)
    if list_id is None:
        await state.clear()
        await message.answer("Не удалось создать список: предмет больше не найден.")
    elif list_id == DUPLICATE_TOPIC_LIST:
        await state.set_state(TopicListForm.waiting_for_title)
        await message.answer("У этого предмета уже есть список с таким названием. Введите другое название или /cancel.")
    else:
        await state.clear()
        await message.answer(
            f"✅ Список «{escape(data['list_title'])}» создан, тем: {len(topics)}.",
            reply_markup=keyboards.to_subject_keyboard(data["subject_id"]),
        )


@router.callback_query(F.data.startswith("ui:tl:add:"))
async def start_adding_topics(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    topic_list = await db.get_topic_list(int(callback_arg(callback)))
    if not topic_list:
        await screens.safe_answer(callback, "Список больше не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(list_id=topic_list["id"], subject_id=topic_list["subject_id"])
    await state.set_state(TopicListForm.waiting_for_more_topics)
    await screens.safe_answer(callback)
    if callback.message:
        await callback.message.answer(
            f"Добавляем темы в список «{escape(topic_list['title'])}».\n"
            "Отправьте новые темы одним сообщением, по одной на строке. Для отмены — /cancel."
        )


@router.message(TopicListForm.waiting_for_more_topics, F.text)
async def add_topics(message: Message, state: FSMContext, db: QueueDB) -> None:
    topics = parse_topics(message.text)
    if topics is None:
        await message.answer(TOPICS_FORMAT_HINT)
        return
    data = await state.get_data()
    added = await db.add_topics(data["list_id"], topics)
    if added is None:
        await state.clear()
        await message.answer("Не удалось добавить темы: список больше не найден.")
        return
    if added == TOPIC_LIST_FULL:
        await message.answer(f"В списке может быть не больше {MAX_TOPICS_PER_LIST} тем. Отправьте меньше тем или /cancel.")
        return
    await state.clear()
    note = f"✅ Добавлено тем: {added}." if added else "Все эти темы уже есть в списке."
    if 0 < added < len(topics):
        note += f" Уже были в списке: {len(topics) - added}."
    await message.answer(note, reply_markup=keyboards.to_subject_keyboard(data["subject_id"]))


# --- administration ------------------------------------------------------------------------------


@router.callback_query(F.data == "ui:admin")
async def admin_panel(callback: CallbackQuery) -> None:
    if not screens.is_admin(callback.from_user.id):
        await screens.safe_answer(callback, "Нет доступа", show_alert=True)
        return
    await screens.show(callback, screens.admin_panel())


@router.callback_query(F.data.startswith("ui:rename:"))
async def start_rename(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    if not screens.is_admin(callback.from_user.id):
        await screens.safe_answer(callback, "Нет доступа", show_alert=True)
        return
    deadline = await db.get_deadline(int(callback_arg(callback)))
    if not deadline:
        await screens.safe_answer(callback, "Очередь больше недоступна.", show_alert=True)
        return
    await state.clear()
    await state.update_data(rename_id=deadline["id"])
    await state.set_state(RenameForm.waiting_for_title)
    await screens.safe_answer(callback)
    if callback.message:
        await callback.message.answer(
            f"Сейчас очередь называется:\n<code>{escape(deadline['title'])}</code>\n\n"
            "Отправьте новое название — можно дописать условие, например "
            "«ПР Моделирование · сдаём ЛР3, не больше 10 человек». Для отмены — /cancel."
        )


@router.message(RenameForm.waiting_for_title, F.text)
async def save_rename(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not message.from_user or not screens.is_admin(message.from_user.id):
        await state.clear()
        return
    title = clean_text(message.text)
    if len(title) not in QUEUE_TITLE_LENGTH:
        await message.answer("Название должно быть от 2 до 200 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    if not await db.rename_queue(data["rename_id"], title):
        await message.answer("Очередь больше недоступна.")
        return
    await message.answer(
        f"✅ Новое название: <b>{escape(title)}</b>", reply_markup=keyboards.to_queue_keyboard(data["rename_id"])
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
