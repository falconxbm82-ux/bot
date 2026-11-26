"""Нижние кнопки ролей и главных действий.

Текст кнопки приходит сообщением. Обработчик стоит раньше мастера,
чтобы «Охрана» не записывалась как госномер.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.engine import Engine

from dockbot.access import active_staff, logout_staff
from dockbot.bot.keyboard import (
    BOOK,
    CANCEL_TRIP,
    DAY_SUMMARY,
    DRIVERS,
    ENTER_PIN,
    FINISHED,
    GATES,
    HELP,
    MONITOR,
    MOVE_TRIP,
    MY_BOOKINGS,
    MY_PASS,
    NAV_MENU,
    ON_SITE,
    ROLE_DRIVER,
    ROLE_GUARD,
    ROLE_WAREHOUSE,
    SCAN,
    SETTINGS,
    SHIFT_TODAY,
    SLOTS,
    STAFF_ENTRY,
    STAFF_LIST,
    WAITING,
    help_text,
    keyboard_for,
)
from dockbot.bot.mine import (
    show_cancel_choices,
    show_move_choices,
    show_my_bookings,
    show_my_pass,
)
from dockbot.bot.admin import (
    open_day,
    open_drivers,
    open_gates,
    open_settings,
    open_slots,
    open_staff,
)
from dockbot.bot.staff import (
    PIN_PROMPT,
    StaffFlow,
    open_checkpoint,
    open_pass_pin,
    open_shift,
)
from dockbot.bot.driver import role_markup, send_role_menu
from dockbot.bot.ui import reply
from dockbot.bot.wizard import show_vehicle
from dockbot.documents import discard_document
from dockbot.models import StaffRole

router = Router(name="roles")

DRIVER_TEXT = "Меню водителя."
GUARD_TEXT = "Меню охраны."
WAREHOUSE_TEXT = "Меню кладовщика."
LOGGED_OUT = "Вы вышли из роли."


@router.message(F.text == ROLE_DRIVER)
async def choose_driver(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    cleared = await asyncio.to_thread(logout_staff, engine, user.id)
    await state.clear()
    await message.answer(
        LOGGED_OUT if cleared else DRIVER_TEXT,
        reply_markup=keyboard_for(None),
    )


@router.message(F.text == ROLE_GUARD)
async def choose_guard(message: Message, state: FSMContext, engine: Engine) -> None:
    await _choose_staff(message, state, engine, StaffRole.security, GUARD_TEXT)


@router.message(F.text == ROLE_WAREHOUSE)
async def choose_warehouse(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    await _choose_staff(message, state, engine, StaffRole.warehouse, WAREHOUSE_TEXT)


@router.callback_query(F.data == NAV_MENU)
async def open_menu(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await send_role_menu(query, state, engine)


@router.message(F.text == "В меню")
async def open_menu_text(message: Message, state: FSMContext, engine: Engine) -> None:
    await send_role_menu(message, state, engine)


@router.callback_query(F.data == "bk:abort")
async def abort_dialog(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    current = await state.get_state()
    if current is None and not data:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    discard_document(data.get("doc_name"))
    await state.clear()
    if current and str(current).startswith("DeskFlow"):
        text = "Запись без чата отменена."
    elif data.get("mode") == "move":
        text = "Диалог сброшен. Бронь не изменена."
    else:
        text = "Диалог сброшен. Запись не сохранена."
    user = query.from_user
    markup = await role_markup(engine, None if user is None else user.id)
    await reply(query, text, markup)


@router.message(F.text == BOOK)
async def choose_book(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.set_data({"mode": "create"})
    await show_vehicle(message, state, engine)


@router.message(F.text == MY_BOOKINGS)
async def choose_bookings(message: Message, engine: Engine) -> None:
    await show_my_bookings(message, engine)


@router.message(F.text == MY_PASS)
async def choose_pass(message: Message, engine: Engine) -> None:
    await show_my_pass(message, engine)


@router.message(F.text == CANCEL_TRIP)
async def choose_cancel(message: Message, engine: Engine) -> None:
    await show_cancel_choices(message, engine)


@router.message(F.text == MOVE_TRIP)
async def choose_move(message: Message, engine: Engine) -> None:
    await show_move_choices(message, engine)


@router.message(F.text == STAFF_ENTRY)
async def choose_staff_entry(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    user = message.from_user
    if user is None:
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is not None:
        await state.clear()
        await message.answer(
            "Вы уже вошли. Чтобы сменить роль, отправьте /logout.",
            reply_markup=keyboard_for(person.role),
        )
        return
    await state.clear()
    await state.set_state(StaffFlow.pin)
    await state.update_data(pin_fails=0)
    await message.answer(PIN_PROMPT)


@router.message(F.text == GATES)
async def choose_gates(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_gates(message, state, engine)


@router.message(F.text == SLOTS)
async def choose_slots(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_slots(message, state, engine)


@router.message(F.text == STAFF_LIST)
async def choose_staff_list(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    await open_staff(message, state, engine)


@router.message(F.text == SETTINGS)
async def choose_settings(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_settings(message, state, engine)


@router.message(F.text == DRIVERS)
async def choose_drivers(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_drivers(message, state, engine)


@router.message(F.text == DAY_SUMMARY)
async def choose_day(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_day(message, state, engine)


@router.message(F.text == HELP)
async def choose_help(message: Message, engine: Engine) -> None:
    user = message.from_user
    person = None
    if user is not None:
        person = await asyncio.to_thread(active_staff, engine, user.id)
    role = None if person is None else person.role
    await message.answer(help_text(role), reply_markup=keyboard_for(role))


@router.message(F.text == SCAN)
async def choose_scan(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_checkpoint(message, state, engine)


@router.message(F.text == ENTER_PIN)
async def choose_pass_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_pass_pin(message, state, engine)


@router.message(F.text == SHIFT_TODAY)
async def choose_guard_shift(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    await open_shift(message, state, engine, "day")


@router.message(F.text == MONITOR)
async def choose_monitor(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_shift(message, state, engine, "day")


@router.message(F.text == WAITING)
async def choose_waiting(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_shift(message, state, engine, "wait")


@router.message(F.text == ON_SITE)
async def choose_onsite(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_shift(message, state, engine, "onsite")


@router.message(F.text == FINISHED)
async def choose_finished(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_shift(message, state, engine, "done")


async def _choose_staff(
    message: Message,
    state: FSMContext,
    engine: Engine,
    role: StaffRole,
    ready_text: str,
) -> None:
    user = message.from_user
    if user is None:
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is not None and person.role is role:
        await state.clear()
        await message.answer(ready_text, reply_markup=keyboard_for(role))
        return
    await state.clear()
    await state.set_state(StaffFlow.pin)
    await state.update_data(wanted_role=role.value, pin_fails=0)
    await message.answer(PIN_PROMPT)
