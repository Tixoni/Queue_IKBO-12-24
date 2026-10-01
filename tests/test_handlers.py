"""Handlers and screens driven with fake Telegram objects and a real temporary database."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from src.bot import handlers, screens
from src.bot.handlers import TopicListForm, parse_topics

from tests.conftest import (
    day, make_callback, make_message, schedule_event, shown_buttons, shown_text,
)


async def lesson_queue(db) -> tuple[dict, int]:
    await db.sync_schedule([schedule_event("k1", day(1), title="Моделирование")])
    [lesson] = await db.list_schedule_for_date(day(1))
    return lesson, await db.get_or_create_schedule_queue("k1", day(1), "Моделирование")


# --- parse_topics ------------------------------------------------------------------


def test_parse_topics_valid():
    assert parse_topics(" Тема 1 \n\nТема 2\n") == ["Тема 1", "Тема 2"]


def test_parse_topics_rejects_bad_input():
    assert parse_topics("") is None
    assert parse_topics("a\na") is None
    assert parse_topics("x" * 81) is None
    assert parse_topics("\n".join(f"t{i}" for i in range(26))) is None
    assert parse_topics("\n".join("y" * 80 for _ in range(23))) is None  # > 1800 chars in total


# --- screens -------------------------------------------------------------------------


async def test_home_screen_for_admin_and_student(db, admin, user):
    callback = make_callback(admin)
    await screens.show_home(callback, db)
    assert "администратор" in shown_text(callback)
    assert ("⚙️ Администрирование", "ui:admin") in shown_buttons(callback)

    callback = make_callback(user)
    await screens.show_home(callback, db)
    assert "Иван Петров" in shown_text(callback)
    assert "ui:admin" not in [data for _, data in shown_buttons(callback)]


async def test_schedule_screen_without_and_with_data(db, user):
    callback = make_callback(user)
    await screens.show_schedule(callback, db)
    assert "ещё не загружено" in shown_text(callback)
    assert "Обновить" not in shown_text(callback)

    await db.sync_schedule([schedule_event("k1", day(1))])
    callback = make_callback(user)
    await screens.show_schedule(callback, db)
    assert "Выберите день" in shown_text(callback)


async def test_lesson_opens_queue_card(db, user):
    lesson, _ = await lesson_queue(db)
    callback = make_callback(user)
    await screens.show_lesson(callback, db, lesson["id"])

    text = shown_text(callback)
    assert "Пара по расписанию" in text
    assert "Пока никого нет" in text
    assert "ID очереди" not in text  # only admins see it
    data = [data for _, data in shown_buttons(callback)]
    assert any(d.startswith("ui:join:") for d in data)
    assert any(d.startswith("ui:topic-list:create:") for d in data)
    assert f"ui:day:{day(1)}" in data


async def test_join_then_leave_updates_card(db, user):
    _, queue_id = await lesson_queue(db)

    callback = make_callback(user, f"ui:join:{queue_id}")
    await handlers.join(callback, db)
    callback.answer.assert_awaited_with("Вы добавлены в очередь.", show_alert=False)
    assert "1. Иван Петров" in shown_text(callback)
    assert any(d.startswith("ui:leave:") for _, d in shown_buttons(callback))

    callback = make_callback(user, f"ui:join:{queue_id}")
    await handlers.join(callback, db)
    callback.answer.assert_awaited_with("Вы уже записаны.", show_alert=True)

    callback = make_callback(user, f"ui:leave:{queue_id}")
    await handlers.leave(callback, db)
    assert "Пока никого нет" in shown_text(callback)


async def test_queue_card_escapes_user_names(db, user):
    _, queue_id = await lesson_queue(db)
    await db.set_user_name(user.id, "<a href='x'>хак</a>")
    await handlers.join(make_callback(user, f"ui:join:{queue_id}"), db)

    callback = make_callback(user)
    await screens.show_queue(callback, db, queue_id)
    assert "&lt;a href=&#x27;x&#x27;&gt;хак&lt;/a&gt;" in shown_text(callback)
    assert "<a href" not in shown_text(callback)


async def test_admin_sees_queue_id(db, admin):
    _, queue_id = await lesson_queue(db)
    callback = make_callback(admin)
    await screens.show_queue(callback, db, queue_id)
    assert f"<code>{queue_id}</code>" in shown_text(callback)


async def test_missing_queue(db, user):
    callback = make_callback(user)
    await screens.show_queue(callback, db, 9999)
    assert "больше недоступна" in shown_text(callback)


async def test_old_button_press_does_not_break_handler(db, user):
    """A button pressed while the bot was offline: Telegram rejects the late answer, the join still counts."""
    _, queue_id = await lesson_queue(db)
    callback = make_callback(user, f"ui:join:{queue_id}")
    callback.answer.side_effect = TelegramBadRequest(EditMessageText(text="x"), "query is too old")

    await handlers.join(callback, db)
    assert await db.get_queue(queue_id) == ["Иван Петров"]
    assert "1. Иван Петров" in shown_text(callback)


async def test_render_ignores_message_not_modified(user):
    callback = make_callback(user)
    callback.message.edit_text.side_effect = TelegramBadRequest(
        EditMessageText(text="x"), "Bad Request: message is not modified"
    )
    await screens.render(callback, "same", None)  # must not raise


# --- name -------------------------------------------------------------------------------


async def test_save_name_cleans_and_validates(db, user, state):
    await state.set_state(handlers.ProfileForm.waiting_for_name)

    message = make_message(user, "x")
    await handlers.save_name(message, state, db)
    assert "от 2 до 60" in message.answer.await_args.args[0]

    await handlers.save_name(make_message(user, "  Тихон‮  К. "), state, db)
    assert await db.get_user_name(user.id) == "Тихон К."
    assert await state.get_state() is None


# --- topic lists ----------------------------------------------------------------------------


async def test_create_and_extend_topic_list(db, user, state):
    _, queue_id = await lesson_queue(db)

    await handlers.start_topic_list(make_callback(user, f"ui:topic-list:create:{queue_id}"), state, db)
    assert await state.get_state() == TopicListForm.waiting_for_title

    await handlers.topic_list_title(make_message(user, "Доклады"), state)
    assert await state.get_state() == TopicListForm.waiting_for_topics

    message = make_message(user, "Тема 1\nТема 2")
    await handlers.topic_list_topics(message, state, db)
    assert "создан, тем: 2" in message.answer.await_args.args[0]
    assert await state.get_state() is None

    callback = make_callback(user)
    await screens.show_queue(callback, db, queue_id)
    assert "Доклады" in shown_text(callback)
    assert ("➕ Дополнить список тем", f"ui:topic-list:add:{queue_id}") in shown_buttons(callback)

    callback = make_callback(user, f"ui:topic-list:add:{queue_id}")
    await handlers.choose_topic_list(callback, db)
    [(_, pick)] = [b for b in shown_buttons(callback) if b[1].startswith("ui:topic-list:pick:")]

    await handlers.start_adding_topics(make_callback(user, pick), state, db)
    assert await state.get_state() == TopicListForm.waiting_for_more_topics

    message = make_message(user, "Тема 2\nТема 3")
    await handlers.add_topics(message, state, db)
    assert "Добавлено тем: 1" in message.answer.await_args.args[0]
    assert (await db.get_topic_lists(queue_id))[0]["topics"] == ["Тема 1", "Тема 2", "Тема 3"]


async def test_duplicate_topic_list_title_asks_again(db, user, state):
    _, queue_id = await lesson_queue(db)
    for expected_state in (None, TopicListForm.waiting_for_title):
        await handlers.start_topic_list(make_callback(user, f"ui:topic-list:create:{queue_id}"), state, db)
        await handlers.topic_list_title(make_message(user, "Список"), state)
        message = make_message(user, "a")
        await handlers.topic_list_topics(message, state, db)
        assert await state.get_state() == expected_state
    assert "уже есть список" in message.answer.await_args.args[0]


async def test_topic_list_for_legacy_queue_is_refused(db, user, state):
    async with db.transaction() as conn:
        cur = await conn.execute("INSERT INTO deadlines(event_date,title) VALUES(?,?)", (day(1), "Старая"))
        queue_id = cur.lastrowid
    callback = make_callback(user, f"ui:topic-list:create:{queue_id}")

    await handlers.start_topic_list(callback, state, db)
    callback.answer.assert_awaited_with("Не удалось определить пару из расписания.", show_alert=True)
    assert await state.get_state() is None


# --- admin -------------------------------------------------------------------------------------


async def test_admin_add_user_command(db, admin, user):
    _, queue_id = await lesson_queue(db)

    message = make_message(user, f"/admin_add_user {queue_id} 5 Вася")
    await handlers.admin_add_user(message, db)
    assert "администраторам" in message.answer.await_args.args[0]

    message = make_message(admin, f"/admin_add_user {queue_id} 5 Вася‮ Пупкин")
    await handlers.admin_add_user(message, db)
    assert "добавлен" in message.answer.await_args.args[0]
    assert await db.get_queue(queue_id) == ["Вася Пупкин"]

    message = make_message(admin, "/admin_add_user abc")
    await handlers.admin_add_user(message, db)
    assert "Формат" in message.answer.await_args.args[0]


async def test_admin_panel_is_hidden_from_students(user):
    callback = make_callback(user, "ui:admin")
    await handlers.admin_panel(callback)
    callback.answer.assert_awaited_with("Нет доступа", show_alert=True)
    callback.message.edit_text.assert_not_awaited()
