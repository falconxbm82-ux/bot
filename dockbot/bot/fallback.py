"""Неизвестный callback старой клавиатуры."""

from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.engine import Engine

from dockbot.access import active_staff
from dockbot.bot.keyboard import keyboard_for

router = Router(name="fallback")


@router.callback_query()
async def stale_button(
    query: CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    await query.answer("Кнопка устарела", show_alert=True)
    await state.clear()
    role = None
    user = query.from_user
    if user is not None:
        person = await asyncio.to_thread(active_staff, engine, user.id)
        if person is not None:
            role = person.role
    message = query.message
    if message is not None and hasattr(message, "answer"):
        await message.answer("Меню.", reply_markup=keyboard_for(role))
