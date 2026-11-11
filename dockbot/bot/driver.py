"""Стартовый экран водителя: забронировать и мои брони.

Шаги мастера, список рейсов и меню персонала в этот срез не входят.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
async def start(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=driver_menu())


@router.callback_query(F.data == BOOK)
async def book_not_ready(query: CallbackQuery) -> None:
    await query.answer("Мастер бронирования ещё не подключён", show_alert=False)


@router.callback_query(F.data == MY_BOOKINGS)
async def bookings_not_ready(query: CallbackQuery) -> None:
    await query.answer("Список броней ещё не подключён", show_alert=False)
