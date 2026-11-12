"""Нижние кнопки ролей и главных действий.

Текст кнопки приходит сообщением. Обработчик стоит раньше мастера,
чтобы «Охрана» не записывалась как госномер.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.engine import Engine

from dockbot.access import active_staff, logout_staff
from dockbot.bot.keyboard import (
    BOOK,
    CHECK_PASS,
    MY_BOOKINGS,
    ROLE_DRIVER,
    ROLE_GUARD,
    ROLE_WAREHOUSE,
    SHIFT_BOARD,
    keyboard_for,
)
from dockbot.bot.mine import show_my_bookings
from dockbot.bot.staff import (
    PIN_PROMPT,
    StaffFlow,
    open_checkpoint,
    open_shift,
)
from dockbot.bot.wizard import show_dates
from dockbot.models import StaffRole

router = Router(name="roles")

DRIVER_TEXT = "Режим водителя."
GUARD_TEXT = "Режим охраны."
WAREHOUSE_TEXT = "Режим кладовщика."
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


@router.message(F.text == BOOK)
async def choose_book(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.set_data({"mode": "create"})
    await show_dates(message, state, engine)


@router.message(F.text == MY_BOOKINGS)
async def choose_bookings(message: Message, engine: Engine) -> None:
    await show_my_bookings(message, engine)


@router.message(F.text == CHECK_PASS)
async def choose_check(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_checkpoint(message, state, engine)


@router.message(F.text == SHIFT_BOARD)
async def choose_shift(message: Message, state: FSMContext, engine: Engine) -> None:
    await open_shift(message, state, engine)


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
    await state.update_data(wanted_role=role.value)
    await message.answer(PIN_PROMPT)
