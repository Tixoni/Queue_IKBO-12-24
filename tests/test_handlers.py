"""Handlers and screens driven with fake Telegram objects and a real temporary database."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from src.bot import handlers

from tests.conftest import (
    day, make_callback, make_message, schedule_event, shown_buttons, shown_text,
)


async def lesson_queue(db) -> tuple[dict, int]:
    await db.sync_schedule([schedule_event("k1", day(1), title="Моделирование")])
    [lesson] = await db.list_schedule_for_date(day(1))
    return lesson, await db.get_or_create_schedule_queue("k1", day(1), "Моделирование")


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


async def test_old_button_press_does_not_break_handler(db, user):
    """A button pressed while the bot was offline: Telegram rejects the late answer, the join still counts."""
    _, queue_id = await lesson_queue(db)
    callback = make_callback(user, f"ui:join:{queue_id}")
    callback.answer.side_effect = TelegramBadRequest(EditMessageText(text="x"), "query is too old")

    await handlers.join(callback, db)
    assert await db.get_queue(queue_id) == ["Иван Петров"]
    assert "1. Иван Петров" in shown_text(callback)


# --- name -------------------------------------------------------------------------------


async def test_save_name_cleans_and_validates(db, user, state):
    await state.set_state(handlers.ProfileForm.waiting_for_name)

    message = make_message(user, "x")
    await handlers.save_name(message, state, db)
    assert "от 2 до 60" in message.answer.await_args.args[0]

    await handlers.save_name(make_message(user, "  Тихон‮  К. "), state, db)
    assert await db.get_user_name(user.id) == "Тихон К."
    assert await state.get_state() is None


async def test_repeat_name_change_is_blocked(db, user, state):
    await state.set_state(handlers.ProfileForm.waiting_for_name)
    await handlers.save_name(make_message(user, "Иван Первый"), state, db)
    assert await db.get_user_name(user.id) == "Иван Первый"

    # Second attempt to submit name
    await state.set_state(handlers.ProfileForm.waiting_for_name)
    msg2 = make_message(user, "Иван Второй")
    await handlers.save_name(msg2, state, db)
    assert "повторно нельзя" in msg2.answer.await_args.args[0]
    assert await db.get_user_name(user.id) == "Иван Первый"

    # Clicking name button when name is already set
    callback = make_callback(user, "ui:name")
    await handlers.name_button(callback, state, db)
    assert "повторно нельзя" in callback.message.answer.await_args.args[0]


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


async def test_admin_set_name_and_allow_change(db, admin, user):
    # Set initial name
    await db.ensure_profile(user.id, "Иван")
    await db.set_user_name(user.id, "Иван Зафиксированный")

    # Regular user cannot use admin command
    msg_user = make_message(user, f"/admin_set_name {user.id} Другое")
    await handlers.admin_set_name(msg_user, db)
    assert "администраторам" in msg_user.answer.await_args.args[0]

    # Admin forces name change
    msg_admin = make_message(admin, f"/admin_set_name {user.id} Новое Имя")
    await handlers.admin_set_name(msg_admin, db)
    assert "изменено" in msg_admin.answer.await_args.args[0]
    assert await db.get_user_name(user.id) == "Новое Имя"

    # Admin allows changing name again
    msg_allow = make_message(admin, f"/admin_allow_name_change {user.id}")
    await handlers.admin_allow_name_change(msg_allow, db)
    assert "разрешено" in msg_allow.answer.await_args.args[0]
    assert await db.can_change_name(user.id)


async def test_admin_panel_is_hidden_from_students(user):
    callback = make_callback(user, "ui:admin")
    await handlers.admin_panel(callback)
    callback.answer.assert_awaited_with("Нет доступа", show_alert=True)
    callback.message.edit_text.assert_not_awaited()
