"""Стартовый экран. Нижняя клавиатура зависит от уже привязанной роли."""

from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy.engine import Engine

from dockbot.bot.keyboard import keyboard_for

router = Router(name="driver")

BOOK = "driver:book"
MY_BOOKINGS = "driver:my_bookings"
START_TEXT = "Управление очередью разгрузки.\nВыберите действие."


def driver_menu() -> ReplyKeyboardMarkup:
    return keyboard_for(None)


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


async def _menu(message: Message, engine: Engine) -> ReplyKeyboardMarkup:
    user = message.from_user
    if user is None:
        return keyboard_for(None)
    from dockbot.access import active_staff

    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        return keyboard_for(None)
    return keyboard_for(person.role)
