"""КПП и экран смены. Обычный чат этих кнопок не видит."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
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
from dockbot.bot.ui import MENU
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
    filter_shift,
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
PIN_BAD = "Неверный PIN."
PIN_LOCKED = "Вход сотрудника заблокирован до /start."
RIGHTS = "Недостаточно прав."
PIN_PROMPT = "Введите PIN персонала."
PASS_PROMPT = "Пришлите фото QR-пропуска."
PASS_PIN_PROMPT = "Введите 6 цифр PIN пропуска."
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
        fails = int(data.get("pin_fails") or 0) + 1
        if fails >= 3:
            await state.clear()
            await message.answer(PIN_LOCKED, reply_markup=keyboard_for(None))
            return
        await state.update_data(pin_fails=fails)
        await message.answer(PIN_BAD)
        return
    await state.clear()
    await message.answer(SIGNED_IN, reply_markup=staff_menu(person.role))


@router.callback_query(F.data == CHECK)
async def ask_pass(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await _reply(query, RIGHTS + " Кладовщик открывает машину из монитора смены.")
        return
    await state.set_state(StaffFlow.checkpoint)
    await _reply(query, PASS_PROMPT)


@router.callback_query(F.data == SHIFT)
async def show_shift(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    await state.set_state(None)
    await _send_shift(query, engine, "day")


@router.callback_query(F.data == "staff:pinpass")
async def ask_pass_pin(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await _reply(query, RIGHTS)
        return
    await state.set_state(StaffFlow.checkpoint)
    await _reply(query, PASS_PIN_PROMPT)


@router.callback_query(F.data.startswith("sh:"))
async def shift_filter(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    raw = query.data or ""
    if raw.startswith("sh:r:"):
        mode = raw.removeprefix("sh:r:")
    else:
        mode = {
            "sh:hour": "hour",
            "sh:day": "day",
            "sh:wait": "wait",
            "sh:onsite": "onsite",
            "sh:done": "done",
        }.get(raw, "day")
    await state.set_state(None)
    await _send_shift(query, engine, mode)


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
        await message.answer(UNREADABLE, reply_markup=_scan_fallback())
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
        await message.answer(
            "Пропуск не найден. Проверьте цифры или пришлите фото QR ещё раз.",
            reply_markup=_scan_fallback(),
        )
        return
    await _show_checkpoint(message, engine, trip)


@router.callback_query(F.data.startswith(OPEN_PREFIX))
async def open_row(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
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
        note = (
            "Статус уже установлен."
            if trip.status == target
            else "Этот переход недоступен. Выберите следующий шаг на карточке."
        )
        await _reply(query, f"{note}\n\n{text}", _markup(trip, card))
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


async def _send_shift(query: CallbackQuery, engine: Engine, mode: str) -> None:
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await _reply(query, RIGHTS)
        return
    except SiteNotConfigured:
        await _reply(query, "Склад не настроен. Обратитесь к администратору стенда.")
        return
    moment = datetime.now(timezone.utc)
    chosen = filter_shift(trips, zone, mode, moment)
    message = query.message
    await query.answer()
    target = message if message is not None and hasattr(message, "answer") else None
    if not chosen and mode != "day":
        text = "В этом списке пусто. Нажмите «Весь день» или «Обновить»."
        markup = _controls(mode)
        if target is None and user is not None:
            await query.bot.send_message(user.id, text, reply_markup=markup)
        elif target is not None:
            await target.answer(text, reply_markup=markup)
        return
    pages = shift_pages(chosen, zone, day)
    await _deliver(query, pages, zone, mode, answered=True)


def _body(engine: Engine, trip: Trip, card: bool) -> str:
    if card:
        return _card(engine, trip)
    from dockbot.booking import load_zone

    return trip_line(trip, load_zone(engine))


def _card(engine: Engine, trip: Trip) -> str:
    from dockbot.booking import load_zone

    return "Пропуск\n" + card_text(trip, load_zone(engine), None)


def _markup(trip: Trip, card: bool) -> InlineKeyboardMarkup | None:
    actions = (*checkpoint_actions(trip), *row_actions(trip))
    rows = [
        [InlineKeyboardButton(text=action.label, callback_data=action.callback)]
        for action in actions
    ]
    if card or not rows:
        rows.append(
            [
                InlineKeyboardButton(text="К списку", callback_data=SHIFT),
                InlineKeyboardButton(text="В меню", callback_data=MENU),
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clip(text: str) -> str:
    if len(text) <= 64:
        return text
    return text[:63] + "…"


def _controls(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Текущий час", callback_data="sh:hour"),
                InlineKeyboardButton(text="Весь день", callback_data="sh:day"),
            ],
            [
                InlineKeyboardButton(text="Обновить", callback_data=f"sh:r:{mode}"),
                InlineKeyboardButton(text="В меню", callback_data=MENU),
            ],
        ]
    )


def _scan_fallback() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести PIN пропуска", callback_data="staff:pinpass"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=MENU)],
        ]
    )


def _with_controls(
    markup: InlineKeyboardMarkup | None, mode: str, first: bool
) -> InlineKeyboardMarkup:
    if not first:
        return markup or _controls(mode)
    extra = list(_controls(mode).inline_keyboard)
    if markup is None:
        return InlineKeyboardMarkup(inline_keyboard=extra)
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, *extra])


async def _deliver(
    query: CallbackQuery,
    pages: list[ShiftPage],
    zone: ZoneInfo,
    mode: str = "day",
    *,
    answered: bool = False,
) -> None:
    message = query.message
    if not answered:
        await query.answer()
    if message is None or not hasattr(message, "answer"):
        if query.from_user is not None:
            for index, page in enumerate(pages):
                await query.bot.send_message(
                    query.from_user.id,
                    page.text,
                    reply_markup=_with_controls(
                        _page_markup(page, zone), mode, index == 0
                    ),
                )
        return
    for index, page in enumerate(pages):
        await message.answer(
            page.text,
            reply_markup=_with_controls(_page_markup(page, zone), mode, index == 0),
        )


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


async def open_pass_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        await message.answer(RIGHTS)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.security:
        await message.answer(
            RIGHTS + " PIN пропуска вводит охрана. Кладовщик берёт машину из списка."
        )
        return
    await state.set_state(StaffFlow.checkpoint)
    await message.answer(PASS_PIN_PROMPT)


async def open_shift(
    message: Message, state: FSMContext, engine: Engine, mode: str = "day"
) -> None:
    user = message.from_user
    if user is None:
        await message.answer(RIGHTS)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await message.answer(RIGHTS + " Сначала откройте роль «Охрана» или «Кладовщик».")
        return
    except SiteNotConfigured:
        await message.answer("Склад не настроен. Обратитесь к администратору стенда.")
        return
    await state.set_state(None)
    moment = datetime.now(timezone.utc)
    chosen = filter_shift(trips, zone, mode, moment)
    if not chosen and mode != "day":
        await message.answer(
            "В этом списке пусто. Нажмите «Весь день» или «Обновить».",
            reply_markup=_controls(mode),
        )
        return
    pages = shift_pages(chosen, zone, day)
    for index, page in enumerate(pages):
        await message.answer(
            page.text,
            reply_markup=_with_controls(_page_markup(page, zone), mode, index == 0),
        )


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
