"""КПП и экран смены. Обычный чат этих кнопок не видит."""

from __future__ import annotations

import asyncio
import io
import logging
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.engine import Engine

from dockbot.access import (
    AccessDenied,
    active_staff,
    enter_pin,
    enter_role,
    logout_staff,
)
from dockbot.bot.driver import driver_menu
from dockbot.bot.keyboard import keyboard_for
from dockbot.booking import SiteNotConfigured, Trip
from dockbot.checkpoint import (
    PassNotFound,
    TransitionRejected,
    apply_mark,
    find_pass,
    load_trip,
    read_pass_image,
)
from dockbot.models import BookingStatus, StaffRole
from dockbot.present import card_text
from dockbot.shiftboard import (
    ShiftPage,
    checkpoint_actions,
    load_shift,
    row_actions,
    shift_pages,
    trip_line,
)

log = logging.getLogger(__name__)

router = Router(name="staff")

CHECK = "staff:check"
SHIFT = "staff:shift"
LOGOUT = "staff:out"
ARRIVED_PREFIX = "sf:a:"
UNLOADING_PREFIX = "sf:u:"
COMPLETED_PREFIX = "sf:c:"
OPEN_PREFIX = "sf:o:"

DENIED = "Доступ не предоставлен."
PIN_PROMPT = "Введите PIN персонала."
PASS_PROMPT = "Пришлите фото пропуска или введите PIN пропуска."
UNREADABLE = (
    "Код на фото не читается. Пришлите другое фото или введите PIN пропуска."
)
NOT_FOUND = "Пропуск не найден."
REJECTED = "Этот переход недоступен."
SIGNED_IN = "Вход выполнен."
LOGGED_OUT = "Вы вышли из роли."


class StaffFlow(StatesGroup):
    pin = State()
    checkpoint = State()


def staff_menu(role: StaffRole):
    return keyboard_for(role)


@router.message(Command("logout"))
async def logout_command(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    await _leave(message, state, engine, user.id)


@router.callback_query(F.data == LOGOUT)
async def logout_button(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return
    await state.clear()
    await asyncio.to_thread(logout_staff, engine, user.id)
    await _reply(query, LOGGED_OUT, driver_menu())


@router.message(Command("staff"))
async def staff_entry(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.set_state(StaffFlow.pin)
        await message.answer(PIN_PROMPT)
        return
    await message.answer(SIGNED_IN, reply_markup=staff_menu(person.role))


@router.message(StaffFlow.pin, F.photo)
async def staff_pin_photo(message: Message) -> None:
    await message.answer(PIN_PROMPT)


@router.message(StaffFlow.pin, F.text)
async def staff_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if user is None or text.startswith("/"):
        return
    data = await state.get_data()
    wanted = data.get("wanted_role")
    try:
        if wanted in (StaffRole.security.value, StaffRole.warehouse.value):
            person = await asyncio.to_thread(
                enter_role, engine, user.id, text, StaffRole(wanted)
            )
        else:
            person = await asyncio.to_thread(enter_pin, engine, user.id, text)
    except AccessDenied:
        await message.answer(DENIED)
        return
    await state.clear()
    await message.answer(SIGNED_IN, reply_markup=staff_menu(person.role))


@router.callback_query(F.data == CHECK)
async def ask_pass(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await _reply(query, DENIED)
        return
    await state.set_state(StaffFlow.checkpoint)
    await _reply(query, PASS_PROMPT)


@router.callback_query(F.data == SHIFT)
async def show_shift(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.warehouse:
        await _reply(query, DENIED)
        return
    await state.set_state(None)
    await _send_shift(query, engine)


@router.message(StaffFlow.checkpoint, F.photo)
async def pass_photo(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    person = await _guard_message(message, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await state.clear()
        await message.answer(DENIED)
        return
    image = await _photo_bytes(message)
    if image is None:
        await message.answer(UNREADABLE)
        return
    try:
        trip = await asyncio.to_thread(read_pass_image, engine, user.id, image)
    except AccessDenied:
        await state.clear()
        await message.answer(DENIED)
        return
    except PassNotFound:
        await message.answer(NOT_FOUND)
        return
    if trip is None:
        log.info("pass qr was not recognized")
        await message.answer(UNREADABLE)
        return
    await _show_checkpoint(message, engine, trip)


@router.message(StaffFlow.checkpoint, F.text)
async def pass_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if user is None or text.startswith("/"):
        return
    person = await _guard_message(message, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await state.clear()
        await message.answer(DENIED)
        return
    try:
        trip = await asyncio.to_thread(find_pass, engine, user.id, pin=text)
    except AccessDenied:
        await state.clear()
        await message.answer(DENIED)
        return
    except PassNotFound:
        await message.answer(NOT_FOUND)
        return
    await _show_checkpoint(message, engine, trip)


@router.callback_query(F.data.startswith(OPEN_PREFIX))
async def open_row(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.warehouse:
        await _reply(query, DENIED)
        return
    booking_id = _id(query.data, OPEN_PREFIX)
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(load_trip, engine, user.id, booking_id)
    except AccessDenied:
        await state.clear()
        await _reply(query, DENIED)
        return
    if trip is None:
        await _reply(query, NOT_FOUND)
        return
    await _show_row(query, engine, trip)


@router.callback_query(F.data.startswith(ARRIVED_PREFIX))
async def mark_arrived(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await _mark(query, state, engine, ARRIVED_PREFIX, BookingStatus.arrived, card=True)


@router.callback_query(F.data.startswith(UNLOADING_PREFIX))
async def mark_unloading(
    query: CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    await _mark(
        query, state, engine, UNLOADING_PREFIX, BookingStatus.unloading, card=False
    )


@router.callback_query(F.data.startswith(COMPLETED_PREFIX))
async def mark_completed(
    query: CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    await _mark(
        query, state, engine, COMPLETED_PREFIX, BookingStatus.completed, card=False
    )


async def _mark(
    query: CallbackQuery,
    state: FSMContext,
    engine: Engine,
    prefix: str,
    target: BookingStatus,
    *,
    card: bool,
) -> None:
    if not await _guard(query, state, engine):
        return
    booking_id = _id(query.data, prefix)
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(
            apply_mark, engine, user.id, booking_id, target
        )
    except AccessDenied:
        await state.clear()
        await _reply(query, DENIED)
        return
    except PassNotFound:
        await _reply(query, NOT_FOUND)
        return
    except TransitionRejected:
        trip = await _current(engine, user.id, booking_id)
        if trip is None:
            await _reply(query, REJECTED)
            return
        text = await asyncio.to_thread(_body, engine, trip, card)
        await _reply(query, f"{REJECTED}\n\n{text}", _markup(trip, card))
        return
    text = await asyncio.to_thread(_body, engine, trip, card)
    await _reply(query, text, _markup(trip, card))


async def _show_checkpoint(message: Message, engine: Engine, trip: Trip) -> None:
    zone_text = await asyncio.to_thread(_card, engine, trip)
    await message.answer(zone_text, reply_markup=_markup(trip, True))


async def _show_row(
    query: CallbackQuery, engine: Engine, trip: Trip
) -> None:
    text = await asyncio.to_thread(_card, engine, trip)
    await _reply(query, text, _markup(trip, False))


async def _send_shift(query: CallbackQuery, engine: Engine) -> None:
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await _reply(query, DENIED)
        return
    except SiteNotConfigured:
        await _reply(query, "Склад не настроен.")
        return
    pages = shift_pages(trips, zone, day)
    await _deliver(query, pages, zone)


def _body(engine: Engine, trip: Trip, card: bool) -> str:
    if card:
        return _card(engine, trip)
    from dockbot.booking import load_zone

    return trip_line(trip, load_zone(engine))


def _card(engine: Engine, trip: Trip) -> str:
    from dockbot.booking import load_zone

    return "Пропуск\n" + card_text(trip, load_zone(engine), None)


def _markup(trip: Trip, card: bool) -> InlineKeyboardMarkup | None:
    actions = checkpoint_actions(trip) if card else row_actions(trip)
    if not actions:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=action.label, callback_data=action.callback)]
            for action in actions
        ]
    )


def _page_markup(page: ShiftPage, zone: ZoneInfo) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for trip in page.trips:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_clip(trip_line(trip, zone)),
                    callback_data=f"{OPEN_PREFIX}{trip.id}",
                )
            ]
        )
        for action in row_actions(trip):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_clip(action.label),
                        callback_data=action.callback,
                    )
                ]
            )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clip(text: str) -> str:
    if len(text) <= 64:
        return text
    return text[:63] + "…"


async def _deliver(
    query: CallbackQuery, pages: list[ShiftPage], zone: ZoneInfo
) -> None:
    message = query.message
    await query.answer()
    if message is None or not hasattr(message, "answer"):
        if query.from_user is not None:
            for page in pages:
                await query.bot.send_message(
                    query.from_user.id,
                    page.text,
                    reply_markup=_page_markup(page, zone),
                )
        return
    for page in pages:
        await message.answer(page.text, reply_markup=_page_markup(page, zone))


async def open_checkpoint(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.security:
        await message.answer(DENIED)
        return
    await state.set_state(StaffFlow.checkpoint)
    await message.answer(PASS_PROMPT)


async def open_shift(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await message.answer(DENIED)
        return
    except SiteNotConfigured:
        await message.answer("Склад не настроен.")
        return
    await state.set_state(None)
    for page in shift_pages(trips, zone, day):
        await message.answer(page.text, reply_markup=_page_markup(page, zone))


async def _leave(
    message: Message, state: FSMContext, engine: Engine, telegram_id: int
) -> None:
    await state.clear()
    await asyncio.to_thread(logout_staff, engine, telegram_id)
    await message.answer(LOGGED_OUT, reply_markup=driver_menu())


async def _guard(query: CallbackQuery, state: FSMContext, engine: Engine):
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return None
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.clear()
        await _reply(query, DENIED)
        return None
    return person


async def _guard_message(message: Message, state: FSMContext, engine: Engine):
    user = message.from_user
    if user is None:
        return None
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.clear()
        await message.answer(DENIED)
        return None
    return person


async def _current(engine: Engine, telegram_id: int, booking_id: int) -> Trip | None:
    try:
        return await asyncio.to_thread(load_trip, engine, telegram_id, booking_id)
    except AccessDenied:
        return None


async def _photo_bytes(message: Message) -> bytes | None:
    if not message.photo:
        return None
    buffer = io.BytesIO()
    try:
        await message.bot.download(message.photo[-1], destination=buffer)
    except Exception:
        log.info("pass photo was not read")
        return None
    return buffer.getvalue()


async def _reply(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    message = query.message
    await query.answer()
    if message is not None and hasattr(message, "answer"):
        await message.answer(text, reply_markup=markup)
        return
    if query.from_user is not None:
        await query.bot.send_message(query.from_user.id, text, reply_markup=markup)


def _id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :]
    if not raw.isdigit():
        return None
    return int(raw)
