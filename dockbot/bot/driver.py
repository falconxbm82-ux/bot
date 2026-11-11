"""Стартовый экран водителя: забронировать и мои брони."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router(name="driver")

BOOK = "driver:book"
MY_BOOKINGS = "driver:my_bookings"
START_TEXT = "Управление очередью разгрузки.\nВыберите действие."


def driver_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Забронировать", callback_data=BOOK)],
            [InlineKeyboardButton(text="Мои брони", callback_data=MY_BOOKINGS)],
        ]
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=driver_menu())


@router.message(Command("cancel"))
async def cancel_dialog(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("mode") == "move":
        text = "Диалог сброшен. Бронь не изменена."
    elif data:
        text = "Диалог сброшен. Запись не сохранена."
    else:
        text = "Диалог сброшен."
    await message.answer(text, reply_markup=driver_menu())
