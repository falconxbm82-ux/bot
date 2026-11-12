"""Стартовый экран. Нижняя клавиатура зависит от уже привязанной роли."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup
from sqlalchemy.engine import Engine

from dockbot.bot.keyboard import CALLBACK_BOOK, CALLBACK_LIST, NAV_MENU, help_text, keyboard_for

router = Router(name="driver")

BOOK = CALLBACK_BOOK
MY_BOOKINGS = CALLBACK_LIST
START_TEXT = "Управление очередью разгрузки.\nВыберите действие."


def driver_menu() -> ReplyKeyboardMarkup:
    return keyboard_for(None)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=await _menu(message, engine))


@router.message(Command("menu"))
async def show_menu(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await message.answer("Меню.", reply_markup=await _menu(message, engine))


@router.message(Command("help"))
async def show_help(message: Message, engine: Engine) -> None:
    from dockbot.access import active_staff

    user = message.from_user
    person = None
    if user is not None:
        person = await asyncio.to_thread(active_staff, engine, user.id)
    role = None if person is None else person.role
    await message.answer(help_text(role), reply_markup=await _menu(message, engine))


@router.callback_query(F.data == NAV_MENU)
async def nav_menu(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    message = query.message
    await query.answer()
    if message is None or not hasattr(message, "answer"):
        return
    await message.answer("Меню.", reply_markup=await _menu_for(query, engine))


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
    return await _keyboard(engine, None if user is None else user.id)


async def _menu_for(query: CallbackQuery, engine: Engine) -> ReplyKeyboardMarkup:
    user = query.from_user
    return await _keyboard(engine, None if user is None else user.id)


async def _keyboard(engine: Engine, telegram_id: int | None) -> ReplyKeyboardMarkup:
    if telegram_id is None:
        return keyboard_for(None)
    from dockbot.access import active_staff

    person = await asyncio.to_thread(active_staff, engine, telegram_id)
    if person is None:
        return keyboard_for(None)
    return keyboard_for(person.role)
