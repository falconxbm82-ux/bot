"""Стартовый экран водителя: забронировать и мои брони.

Кнопки охраны и кладовщика добавляются только если Telegram ID
уже привязан к действующему сотруднику.
"""

from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.engine import Engine

router = Router(name="driver")

BOOK = "driver:book"
MY_BOOKINGS = "driver:my_bookings"
START_TEXT = "Управление очередью разгрузки.\nВыберите действие."


def driver_menu(
    extra: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Забронировать", callback_data=BOOK)],
        [InlineKeyboardButton(text="Мои брони", callback_data=MY_BOOKINGS)],
    ]
    if extra:
        rows.extend(extra)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=await _menu(message, engine))


@router.message(Command("cancel"))
async def cancel_dialog(message: Message, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("mode") == "move":
        text = "Диалог сброшен. Бронь не изменена."
    elif data:
        text = "Диалог сброшен. Запись не сохранена."
    else:
        text = "Диалог сброшен."
    await message.answer(text, reply_markup=await _menu(message, engine))


async def _menu(message: Message, engine: Engine) -> InlineKeyboardMarkup:
    user = message.from_user
    if user is None:
        return driver_menu()
    from dockbot.access import active_staff
    from dockbot.bot.staff import menu_rows

    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        return driver_menu()
    return driver_menu(menu_rows())
