import logging
from html import escape

import httpx
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src import topics_import
from src.bot import keyboards, screens
from src.bot.formatting import clean_text
from src.db import (
    ALREADY_HAS_TOPIC, DUPLICATE_TOPIC_LIST, JOINED, LIST_MISSING, MAX_TOPICS_PER_LIST, TAKEN, TOPIC_BUSY,
    TOPIC_LIST_FULL_STATUS, QueueDB,
)

log = logging.getLogger(__name__)
router = Router()

NAME_LENGTH = range(2, 61)
QUEUE_TITLE_LENGTH = range(2, 201)
TOPIC_LIST_TITLE_LENGTH = range(2, 81)
TOPIC_LENGTH = range(2, 81)


class ProfileForm(StatesGroup):
    waiting_for_name = State()


class TopicListForm(StatesGroup):
    waiting_for_title = State()
    waiting_for_my_topic = State()


class AdminTopicForm(StatesGroup):
    waiting_for_list_title = State()
    waiting_for_topic_text = State()


class RenameForm(StatesGroup):
    waiting_for_title = State()


def callback_arg(callback: CallbackQuery) -> str:
    """Last ':'-separated part of the callback data (an id or an ISO date)."""
    return callback.data.rsplit(":", 1)[1]


async def ask_for_name(message: Message, state: FSMContext, user_id: int, db: QueueDB) -> None:
    if not await db.can_change_name(user_id):
        current_name = await db.get_user_name(user_id) or "не указано"
        await message.answer(
            f"❌ Вы уже установили имя: <b>{escape(current_name)}</b>.\n\n"
            "Менять имя повторно нельзя. Если вам нужно изменить имя, обратитесь к старосте или администратору."
        )
        return
    await state.set_state(ProfileForm.waiting_for_name)
    await message.answer(
        "Напишите имя и фамилию для очереди (например, <i>Иванов Иван</i>).\n\n"
        "⚠️ <b>Внимание:</b> имя можно установить только один раз! Повторная смена будет недоступна.\n"
        "Чтобы отменить, отправьте /cancel."
    )


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
async def panel_name(message: Message, state: FSMContext, db: QueueDB) -> None:
    await state.clear()
    if message.from_user:
        await ask_for_name(message, state, message.from_user.id, db)


# --- commands and navigation ---------------------------------------------------------------------


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not message.from_user:
        return
    await state.clear()
    name = clean_text(message.from_user.full_name)[:60] or "Пользователь"
    await db.ensure_profile(message.from_user.id, name)
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
async def join(callback: CallbackQuery, db: QueueDB, bot: Bot) -> None:
    deadline_id = int(callback_arg(callback))
    result = await db.join(deadline_id, callback.from_user.id, await screens.display_name(db, callback.from_user))
    await screens.safe_answer(callback, result, show_alert=result != JOINED)
    await screens.show(callback, await screens.queue(db, callback.from_user, deadline_id), answer=False)
    if result == JOINED:
        await send_premium_alerts(bot, db, deadline_id)


async def send_premium_alerts(bot: Bot, db: QueueDB, deadline_id: int) -> None:
    """Tell premium users that people started joining a queue they are not in (once per queue)."""
    recipients = await db.claim_premium_alerts(deadline_id)
    if not recipients:
        return
    alert = await screens.premium_alert(db, deadline_id)
    if alert is None:
        return
    for user_id in recipients:
        try:
            await bot.send_message(user_id, alert.text, reply_markup=alert.keyboard)
        except TelegramAPIError as exc:  # blocked the bot, never pressed /start, etc.
            log.warning("Premium alert to %s failed: %s", user_id, exc)


@router.callback_query(F.data.startswith("ui:leave:"))
async def leave(callback: CallbackQuery, db: QueueDB) -> None:
    deadline_id = int(callback_arg(callback))
    left = await db.leave(deadline_id, callback.from_user.id)
    await screens.safe_answer(callback, "Вы вышли из очереди." if left else "Вас уже нет в этой очереди.")
    await screens.show(callback, await screens.queue(db, callback.from_user, deadline_id), answer=False)


# --- profile name ------------------------------------------------------------------------------


@router.callback_query(F.data == keyboards.NAME)
async def name_button(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    await screens.safe_answer(callback)
    if callback.message and callback.from_user:
        await ask_for_name(callback.message, state, callback.from_user.id, db)


@router.message(ProfileForm.waiting_for_name, F.text)
async def save_name(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not message.from_user:
        return
    name = clean_text(message.text)
    if len(name) not in NAME_LENGTH:
        await message.answer("Имя должно быть от 2 до 60 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    saved = await db.set_user_name(message.from_user.id, name)
    await state.clear()
    if not saved:
        await message.answer("❌ Менять имя повторно нельзя. По вопросам изменения обратитесь к администратору.")
    else:
        await message.answer(f"✅ Имя для очереди сохранено: <b>{escape(name)}</b>\nПовторная смена имени заблокирована.")
    await screens.send(message, await screens.home(db, message.from_user))


# --- topic lists by subject ----------------------------------------------------------------------


@router.callback_query(F.data == keyboards.TOPICS)
async def subjects(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.subjects(db))


@router.callback_query(F.data == keyboards.PICK_SUBJECT)
async def pick_subject(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.pick_subject(db))


@router.callback_query(F.data == keyboards.ALL_TOPICS)
async def all_topic_lists(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.safe_answer(callback)
    if callback.message:
        for screen in await screens.all_topic_lists(db):
            await screens.send(callback.message, screen)


@router.callback_query(F.data.startswith("ui:subj:"))
async def subject(callback: CallbackQuery, db: QueueDB) -> None:
    await screens.show(callback, await screens.subject(db, int(callback_arg(callback)), callback.from_user))


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
async def topic_list_title(message: Message, state: FSMContext, db: QueueDB) -> None:
    title = clean_text(message.text)
    if len(title) not in TOPIC_LIST_TITLE_LENGTH:
        await message.answer("Название списка должно быть от 2 до 80 символов. Попробуйте ещё раз или отправьте /cancel.")
        return
    data = await state.get_data()
    list_id = await db.create_topic_list(data["subject_id"], title, message.from_user.id)
    if list_id is None:
        await state.clear()
        await message.answer("Не удалось создать список: предмет больше не найден.")
    elif list_id == DUPLICATE_TOPIC_LIST:
        await message.answer("У этого предмета уже есть список с таким названием. Введите другое название или /cancel.")
    else:
        await state.clear()
        await message.answer(
            f"✅ Список «{escape(title)}» создан. Теперь каждый может занять в нём одну тему.",
            reply_markup=keyboards.take_topic_keyboard(list_id, data["subject_id"]),
        )


@router.callback_query(F.data.startswith("ui:tl:take:"))
async def start_taking_topic(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    topic_list = await db.get_topic_list(int(callback_arg(callback)))
    if not topic_list:
        await screens.safe_answer(callback, "Список больше не найден.", show_alert=True)
        return
    mine = await db.get_user_topic(topic_list["id"], callback.from_user.id)
    if mine:
        await screens.safe_answer(
            callback, f"Вы уже заняли тему «{mine}» в этом списке. Изменить её может админ.", show_alert=True
        )
        return
    await state.clear()
    await state.update_data(list_id=topic_list["id"], subject_id=topic_list["subject_id"])
    await state.set_state(TopicListForm.waiting_for_my_topic)
    await screens.safe_answer(callback)
    if callback.message:
        await callback.message.answer(
            f"Список «{escape(topic_list['title'])}».\n"
            "Напишите одной строкой тему, которую берёте (можно занять только одну). Для отмены — /cancel."
        )


@router.message(TopicListForm.waiting_for_my_topic, F.text)
async def take_topic(message: Message, state: FSMContext, db: QueueDB) -> None:
    topic = clean_text(message.text)
    if "\n" in message.text.strip() or len(topic) not in TOPIC_LENGTH:
        await message.answer("Нужна одна тема одной строкой, от 2 до 80 символов. Попробуйте ещё раз или /cancel.")
        return
    data = await state.get_data()
    result = await db.take_topic(
        data["list_id"], message.from_user.id, await screens.display_name(db, message.from_user), topic
    )
    if result == TOPIC_BUSY:
        await message.answer("Эту тему уже заняли. Выберите другую или отправьте /cancel.")
        return
    await state.clear()
    replies = {
        TAKEN: f"✅ Тема «{escape(topic)}» закреплена за вами.",
        ALREADY_HAS_TOPIC: "Вы уже заняли тему в этом списке. Изменить её может админ.",
        TOPIC_LIST_FULL_STATUS: f"В списке уже {MAX_TOPICS_PER_LIST} тем — больше добавить нельзя.",
        LIST_MISSING: "Список больше не найден.",
    }
    await message.answer(replies[result], reply_markup=keyboards.to_subject_keyboard(data["subject_id"]))


# --- topic lists: admin editing ----------------------------------------------------------------


async def _admin_only(callback: CallbackQuery) -> bool:
    if screens.is_admin(callback.from_user.id):
        return True
    await screens.safe_answer(callback, "Нет доступа", show_alert=True)
    return False


@router.callback_query(F.data.startswith("ui:tl:edit:"))
async def edit_topic_list(callback: CallbackQuery, db: QueueDB) -> None:
    if await _admin_only(callback):
        await screens.show(callback, await screens.edit_topic_list(db, int(callback_arg(callback))))


@router.callback_query(F.data.startswith("ui:tl:rename:"))
async def start_list_rename(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    if not await _admin_only(callback):
        return
    topic_list = await db.get_topic_list(int(callback_arg(callback)))
    if not topic_list:
        await screens.safe_answer(callback, "Список больше не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(list_id=topic_list["id"])
    await state.set_state(AdminTopicForm.waiting_for_list_title)
    await screens.safe_answer(callback)
    if callback.message:
        await callback.message.answer(
            f"Сейчас список называется:\n<code>{escape(topic_list['title'])}</code>\n\n"
            "Отправьте новое название. Для отмены — /cancel."
        )


@router.message(AdminTopicForm.waiting_for_list_title, F.text)
async def save_list_rename(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not screens.is_admin(message.from_user.id):
        await state.clear()
        return
    title = clean_text(message.text)
    if len(title) not in TOPIC_LIST_TITLE_LENGTH:
        await message.answer("Название должно быть от 2 до 80 символов. Попробуйте ещё раз или /cancel.")
        return
    data = await state.get_data()
    result = await db.rename_topic_list(data["list_id"], title)
    if result == "duplicate":
        await message.answer("У предмета уже есть список с таким названием. Введите другое или /cancel.")
        return
    await state.clear()
    text = f"✅ Список переименован: <b>{escape(title)}</b>" if result == "ok" else "Список больше не найден."
    await message.answer(text, reply_markup=keyboards.to_edit_list_keyboard(data["list_id"]))


@router.callback_query(F.data.startswith("ui:ti:edit:"))
async def start_topic_edit(callback: CallbackQuery, state: FSMContext, db: QueueDB) -> None:
    if not await _admin_only(callback):
        return
    item = await db.get_topic_item(int(callback_arg(callback)))
    if not item:
        await screens.safe_answer(callback, "Тема больше не найдена.", show_alert=True)
        return
    await state.clear()
    await state.update_data(item_id=item["id"], list_id=item["list_id"])
    await state.set_state(AdminTopicForm.waiting_for_topic_text)
    await screens.safe_answer(callback)
    owner = f" (занял(а): {escape(item['owner_name'])})" if item["owner_name"] else ""
    if callback.message:
        await callback.message.answer(
            f"Тема{owner}:\n<code>{escape(item['topic'])}</code>\n\n"
            "Отправьте новый текст темы, или «-», чтобы удалить её. Для отмены — /cancel."
        )


@router.message(AdminTopicForm.waiting_for_topic_text, F.text)
async def save_topic_edit(message: Message, state: FSMContext, db: QueueDB) -> None:
    if not screens.is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    if message.text.strip() == "-":
        await state.clear()
        deleted = await db.delete_topic(data["item_id"])
        await message.answer("🗑 Тема удалена." if deleted else "Тема уже удалена.",
                             reply_markup=keyboards.to_edit_list_keyboard(data["list_id"]))
        return
    topic = clean_text(message.text)
    if len(topic) not in TOPIC_LENGTH:
        await message.answer("Тема должна быть от 2 до 80 символов. Попробуйте ещё раз, «-» для удаления или /cancel.")
        return
    result = await db.edit_topic(data["item_id"], topic)
    if result == "duplicate":
        await message.answer("Такая тема в списке уже есть. Введите другую или /cancel.")
        return
    await state.clear()
    text = f"✅ Тема изменена: <b>{escape(topic)}</b>" if result == "ok" else "Тема больше не найдена."
    await message.answer(text, reply_markup=keyboards.to_edit_list_keyboard(data["list_id"]))


@router.message(Command("import_topics"))
async def import_topics(message: Message, db: QueueDB) -> None:
    if not message.from_user or not screens.is_admin(message.from_user.id):
        await message.answer("Команда доступна администраторам.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Формат: /import_topics ID_списка ссылка_на_Google_Таблицу\n"
            "В таблице: первый столбец — ФИО, второй — тема. ID списка видно в разделе «📌 Темы»."
        )
        return
    list_id = int(parts[1])
    topic_list = await db.get_topic_list(list_id)
    if not topic_list:
        await message.answer(f"Списка с ID {list_id} нет.")
        return
    try:
        rows = await topics_import.fetch_rows(parts[2].strip())
    except (ValueError, httpx.HTTPError) as exc:
        await message.answer(f"Не удалось прочитать таблицу: {escape(str(exc) or type(exc).__name__)}")
        return
    result = await db.import_topics(list_id, rows)
    if result is None:
        await message.answer("Список больше не найден.")
        return
    added, skipped = result
    note = f" Пропущено: {skipped} (тема уже занята или у человека уже есть тема)." if skipped else ""
    await message.answer(
        f"✅ В список «{escape(topic_list['title'])}» добавлено тем: {added}.{note}",
        reply_markup=keyboards.to_subject_keyboard(topic_list["subject_id"]),
    )


# --- administration ------------------------------------------------------------------------------


@router.callback_query(F.data == "ui:admin")
async def admin_panel(callback: CallbackQuery) -> None:
    if not screens.is_admin(callback.from_user.id):
        await screens.safe_answer(callback, "Нет доступа", show_alert=True)
        return
    await screens.show(callback, screens.admin_panel())


@router.callback_query(F.data == keyboards.PREMIUM)
async def premium_panel(callback: CallbackQuery, db: QueueDB) -> None:
    if not screens.is_admin(callback.from_user.id):
        await screens.safe_answer(callback, "Нет доступа", show_alert=True)
        return
    await screens.show(callback, await screens.premium(db))


@router.callback_query(F.data.startswith("ui:prem:"))
async def toggle_premium(callback: CallbackQuery, db: QueueDB) -> None:
    if not screens.is_admin(callback.from_user.id):
        await screens.safe_answer(callback, "Нет доступа", show_alert=True)
        return
    enabled = await db.toggle_premium(int(callback_arg(callback)))
    note = {True: "⭐ Уведомления включены", False: "Уведомления выключены", None: "Пользователь не найден"}[enabled]
    await screens.safe_answer(callback, note)
    await screens.show(callback, await screens.premium(db), answer=False)


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
async def admin_add_user(message: Message, db: QueueDB, bot: Bot) -> None:
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
    if added:
        await send_premium_alerts(bot, db, int(deadline_id))


@router.message(Command("admin_set_name"))
async def admin_set_name(message: Message, db: QueueDB) -> None:
    if not message.from_user or not screens.is_admin(message.from_user.id):
        await message.answer("Команда доступна администраторам.")
        return
    try:
        _, user_id_str, name = message.text.split(maxsplit=2)
        user_id = int(user_id_str)
        cleaned_name = clean_text(name)[:60]
    except (ValueError, AttributeError):
        await message.answer("Формат: /admin_set_name TELEGRAM_ID Имя")
        return
    if len(cleaned_name) not in NAME_LENGTH:
        await message.answer("Имя должно быть от 2 до 60 символов.")
        return
    await db.set_user_name(user_id, cleaned_name, is_admin_override=True)
    await message.answer(f"✅ Имя пользователя <code>{user_id}</code> изменено на <b>{escape(cleaned_name)}</b>.")


@router.message(Command("admin_allow_name_change"))
async def admin_allow_name_change(message: Message, db: QueueDB) -> None:
    if not message.from_user or not screens.is_admin(message.from_user.id):
        await message.answer("Команда доступна администраторам.")
        return
    try:
        _, user_id_str = message.text.split(maxsplit=1)
        user_id = int(user_id_str)
    except (ValueError, AttributeError):
        await message.answer("Формат: /admin_allow_name_change TELEGRAM_ID")
        return
    ok = await db.allow_name_change(user_id)
    if ok:
        await message.answer(f"✅ Пользователю <code>{user_id}</code> разрешено повторно изменить имя.")
    else:
        await message.answer(f"❌ Пользователь <code>{user_id}</code> не найден в базе данных.")
