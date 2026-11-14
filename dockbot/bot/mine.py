"""Будущие брони этого чата."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.engine import Engine

from dockbot.booking import (
    BookingError,
    BookingNotFound,
    FieldError,
    NotEditable,
    SiteNotConfigured,
    WindowClosed,
    cancel_booking,
    change_block,
    clean_cargo,
    clean_driver_name,
    clean_make,
    get_owned,
    normalize_plate,
    list_live,
    load_location,
    load_zone,
    queue_note,
    update_vehicle,
)
from dockbot.bot.driver import MY_BOOKINGS, driver_menu
from dockbot.bot.ui import (
    CARGO_PREFIX,
    CANCEL_NO_PREFIX,
    CANCEL_PREFIX,
    CANCEL_YES_PREFIX,
    MOVE_PREFIX,
    OPEN_PREFIX,
    PAGE_PREFIX,
    PASS_PREFIX,
    actor_ids,
    cancel_markup,
    GEO,
    PARTY_PREFIX,
    card_markup,
    cancel_step_markup,
    cargo_markup,
    empty_bookings_markup,
    reply,
    trips_markup,
)
from dockbot.bot.wizard import begin_move
from dockbot.passes import qr_png
from dockbot.present import card_text, local_parts, pass_caption

log = logging.getLogger(__name__)
router = Router(name="mine")

_CARGO_LABELS = {
    "pallets": "Паллеты",
    "bulk": "Навал",
    "liquid": "Жидкость",
    "reefer": "Рефрижератор",
}


class PartyFlow(StatesGroup):
    plate = State()
    make = State()
    driver_name = State()
    cargo = State()
    cargo_note = State()


@router.callback_query(F.data == MY_BOOKINGS)
async def my_bookings(query: CallbackQuery, engine: Engine) -> None:
    await _show_list(query, engine, 0)


async def show_my_bookings(message: Message, engine: Engine) -> None:
    await _message_list(message, engine, "Активные брони.")


async def show_my_pass(message: Message, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        trips = await asyncio.to_thread(list_live, engine, user.id, message.chat.id)
        zone = await asyncio.to_thread(load_zone, engine)
    except SiteNotConfigured as exc:
        await message.answer(str(exc))
        return
    if not trips:
        await message.answer(
            "Активных броней нет. Нажмите «Записаться».",
            reply_markup=empty_bookings_markup(),
        )
        return
    if len(trips) == 1:
        trip = trips[0]
        block = await asyncio.to_thread(change_block, engine, trip)
        await message.answer(
            await _described(engine, trip, zone, block),
            reply_markup=card_markup(trip, block is None),
        )
        return
    await message.answer("Выберите бронь.", reply_markup=trips_markup(trips, zone, 0))


async def show_directions(message: Message, engine: Engine) -> None:
    point = await asyncio.to_thread(load_location, engine)
    if point is None:
        await message.answer(
            "Геометка склада не задана. Запишитесь кнопкой «Записаться» — "
            "пропуск придёт в этот чат.",
            reply_markup=empty_bookings_markup(),
        )
        return
    await message.answer("Склад. Точка на карте ниже. Пропуск — в «Мой пропуск».")
    await message.answer_location(latitude=point[0], longitude=point[1])


async def show_cancel_choices(message: Message, engine: Engine) -> None:
    await _change_list(
        message,
        engine,
        title="Какую бронь отменить?",
        empty=(
            "Сейчас отменять нечего. Окно закрывается за 2 часа до слота. "
            "Откройте «Мои брони»."
        ),
    )


async def show_move_choices(message: Message, engine: Engine) -> None:
    await _change_list(
        message,
        engine,
        title="Какую бронь перенести?",
        empty=(
            "Сейчас переносить нечего. Окно закрывается за 2 часа до слота. "
            "Откройте «Мои брони»."
        ),
    )


async def _message_list(message: Message, engine: Engine, title: str) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        trips = await asyncio.to_thread(list_live, engine, user.id, message.chat.id)
        zone = await asyncio.to_thread(load_zone, engine)
    except SiteNotConfigured as exc:
        await message.answer(str(exc))
        return
    if not trips:
        await message.answer(
            "Активных броней нет. Нажмите «Записаться».",
            reply_markup=empty_bookings_markup(),
        )
        return
    await message.answer(title, reply_markup=trips_markup(trips, zone, 0))


async def _change_list(
    message: Message, engine: Engine, *, title: str, empty: str
) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        trips = await asyncio.to_thread(list_live, engine, user.id, message.chat.id)
        zone = await asyncio.to_thread(load_zone, engine)
        open_trips = []
        for trip in trips:
            if await asyncio.to_thread(change_block, engine, trip) is None:
                open_trips.append(trip)
    except SiteNotConfigured as exc:
        await message.answer(str(exc))
        return
    if not open_trips:
        await message.answer(empty, reply_markup=empty_bookings_markup())
        return
    await message.answer(title, reply_markup=trips_markup(open_trips, zone, 0))


@router.callback_query(F.data.startswith(PAGE_PREFIX))
async def more_bookings(query: CallbackQuery, engine: Engine) -> None:
    raw = query.data.removeprefix(PAGE_PREFIX)
    offset = int(raw) if raw.isdigit() else 0
    await _show_list(query, engine, offset)


@router.callback_query(F.data.startswith(OPEN_PREFIX))
async def open_card(query: CallbackQuery, engine: Engine) -> None:
    booking_id = _id(query.data, OPEN_PREFIX)
    if booking_id is None:
        await query.answer()
        return
    await _show_card(query, engine, booking_id)


@router.callback_query(F.data == GEO)
async def show_geo(query: CallbackQuery, engine: Engine) -> None:
    message = query.message
    await query.answer()
    if message is None or not hasattr(message, "answer"):
        return
    point = await asyncio.to_thread(load_location, engine)
    if point is None:
        await message.answer(
            "Геометка склада не задана. Пропуск остаётся в этом чате."
        )
        return
    await message.answer_location(latitude=point[0], longitude=point[1])


@router.callback_query(F.data.startswith(PASS_PREFIX))
async def show_pass(query: CallbackQuery, engine: Engine) -> None:
    booking_id = _id(query.data, PASS_PREFIX)
    ids = actor_ids(query)
    if booking_id is None or ids is None:
        await query.answer()
        return
    try:
        trip = await asyncio.to_thread(get_owned, engine, ids[0], ids[1], booking_id)
        zone = await asyncio.to_thread(load_zone, engine)
    except BookingNotFound as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    message = query.message
    await query.answer()
    if message is None or not hasattr(message, "answer_photo"):
        return
    try:
        await message.answer_photo(
            BufferedInputFile(qr_png(trip.pass_token), filename="pass.png"),
            caption=pass_caption(trip, zone),
        )
    except Exception:
        log.warning("pass image was not delivered for booking %s", trip.id)
        await message.answer(
            "Пропуск не отправился. Нажмите «Показать пропуск» ещё раз."
        )


@router.callback_query(F.data.startswith(CANCEL_PREFIX))
async def ask_cancel(query: CallbackQuery, engine: Engine) -> None:
    booking_id = _id(query.data, CANCEL_PREFIX)
    ids = actor_ids(query)
    if booking_id is None or ids is None:
        await query.answer()
        return
    try:
        trip = await asyncio.to_thread(get_owned, engine, ids[0], ids[1], booking_id)
        block = await asyncio.to_thread(change_block, engine, trip)
        zone = await asyncio.to_thread(load_zone, engine)
    except BookingNotFound as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    if block:
        await reply(query, await _described(engine, trip, zone, block), card_markup(trip, False))
        return
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    await reply(
        query,
        f"Отменить бронь на {date_text} {clock}, ворота №{trip.gate_code}?",
        cancel_markup(trip.id),
    )


@router.callback_query(F.data.startswith(CANCEL_NO_PREFIX))
async def keep_booking(query: CallbackQuery, engine: Engine) -> None:
    booking_id = _id(query.data or "", CANCEL_NO_PREFIX)
    if booking_id is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await _show_card(query, engine, booking_id)


@router.callback_query(F.data.startswith(CANCEL_YES_PREFIX))
async def do_cancel(query: CallbackQuery, engine: Engine) -> None:
    booking_id = _id(query.data, CANCEL_YES_PREFIX)
    ids = actor_ids(query)
    if booking_id is None or ids is None:
        await query.answer()
        return
    try:
        await asyncio.to_thread(
            cancel_booking,
            engine,
            telegram_id=ids[0],
            chat_id=ids[1],
            booking_id=booking_id,
        )
    except BookingNotFound as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except (WindowClosed, NotEditable, SiteNotConfigured) as exc:
        await reply(query, str(exc))
        return
    await reply(query, "Бронь отменена. Слот снова свободен.", driver_menu())


@router.callback_query(F.data.startswith(MOVE_PREFIX))
async def start_move(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    booking_id = _id(query.data, MOVE_PREFIX)
    ids = actor_ids(query)
    if booking_id is None or ids is None:
        await query.answer()
        return
    try:
        trip = await asyncio.to_thread(get_owned, engine, ids[0], ids[1], booking_id)
        block = await asyncio.to_thread(change_block, engine, trip)
        zone = await asyncio.to_thread(load_zone, engine)
    except BookingNotFound as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    if block:
        await reply(query, await _described(engine, trip, zone, block), card_markup(trip, False))
        return
    await begin_move(query, state, engine, booking_id)


async def _show_list(query: CallbackQuery, engine: Engine, offset: int) -> None:
    ids = actor_ids(query)
    if ids is None:
        await query.answer()
        return
    try:
        trips = await asyncio.to_thread(list_live, engine, ids[0], ids[1])
        zone = await asyncio.to_thread(load_zone, engine)
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    if not trips:
        await reply(
            query,
            "Активных броней нет. Нажмите «Записаться».",
            empty_bookings_markup(),
        )
        return
    if offset < 0 or offset >= len(trips):
        offset = 0
    await reply(query, "Будущие брони.", trips_markup(trips, zone, offset))


async def _show_card(query: CallbackQuery, engine: Engine, booking_id: int) -> None:
    ids = actor_ids(query)
    if ids is None:
        await query.answer()
        return
    try:
        trip = await asyncio.to_thread(get_owned, engine, ids[0], ids[1], booking_id)
        block = await asyncio.to_thread(change_block, engine, trip)
        zone = await asyncio.to_thread(load_zone, engine)
    except BookingNotFound as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    await reply(query, await _described(engine, trip, zone, block), card_markup(trip, block is None))


@router.callback_query(F.data.startswith(PARTY_PREFIX))
async def start_party(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    ids = actor_ids(query)
    booking_id = _id(query.data or "", PARTY_PREFIX)
    if ids is None or booking_id is None:
        await query.answer()
        return
    try:
        trip = await asyncio.to_thread(get_owned, engine, ids[0], ids[1], booking_id)
        block = await asyncio.to_thread(change_block, engine, trip)
    except BookingError as exc:
        await reply(query, str(exc), driver_menu())
        return
    if block is not None:
        await reply(query, block, card_markup(trip, False))
        return
    await state.set_state(PartyFlow.plate)
    await state.update_data(booking_id=booking_id)
    await reply(query, "Новый госномер, например А123ВС178.", cancel_step_markup())


@router.message(PartyFlow.plate, F.text)
async def party_plate(message: Message, state: FSMContext) -> None:
    try:
        plate = normalize_plate(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(plate=plate)
    await state.set_state(PartyFlow.make)
    await message.answer("Марка.", reply_markup=cancel_step_markup())


@router.message(PartyFlow.make, F.text)
async def party_make(message: Message, state: FSMContext) -> None:
    try:
        field = clean_make(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(make=field.value)
    await state.set_state(PartyFlow.driver_name)
    await message.answer("ФИО водителя.", reply_markup=cancel_step_markup())


@router.message(PartyFlow.driver_name, F.text)
async def party_name(message: Message, state: FSMContext) -> None:
    try:
        field = clean_driver_name(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(driver_name=field.value)
    await state.set_state(PartyFlow.cargo)
    await message.answer("Тип груза.", reply_markup=cargo_markup())


@router.callback_query(PartyFlow.cargo, F.data.startswith(CARGO_PREFIX))
async def party_cargo(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    code = (query.data or "").removeprefix(CARGO_PREFIX)
    if code == "other":
        await state.set_state(PartyFlow.cargo_note)
        await reply(query, "Напишите тип груза.", cancel_step_markup())
        return
    label = _CARGO_LABELS.get(code)
    if label is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await _save_party(query, state, engine, label)


@router.message(PartyFlow.cargo_note, F.text)
async def party_cargo_note(message: Message, state: FSMContext, engine: Engine) -> None:
    try:
        field = clean_cargo(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await _save_party(message, state, engine, field.value)


async def _save_party(
    event: Message | CallbackQuery, state: FSMContext, engine: Engine, cargo: str
) -> None:
    ids = actor_ids(event)
    data = await state.get_data()
    if ids is None or not data.get("booking_id"):
        await state.clear()
        await reply(event, "Замена устарела. Откройте «Мои брони».", driver_menu())
        return
    try:
        trip = await asyncio.to_thread(
            update_vehicle,
            engine,
            telegram_id=ids[0],
            chat_id=ids[1],
            booking_id=int(data["booking_id"]),
            plate=data.get("plate", ""),
            make=data.get("make", ""),
            driver_name=data.get("driver_name", ""),
            cargo_type=cargo,
        )
    except BookingError as exc:
        await state.clear()
        await reply(event, str(exc), driver_menu())
        return
    await state.clear()
    zone = await asyncio.to_thread(load_zone, engine)
    await reply(
        event,
        "Данные заменены. QR и PIN пропуска прежние.\n" + card_text(trip, zone, None),
        card_markup(trip, True),
    )


async def _described(engine: Engine, trip, zone, block: str | None) -> str:
    text = card_text(trip, zone, block)
    note = await asyncio.to_thread(queue_note, engine, trip)
    if note:
        return f"{text}\n{note}"
    return text


def _id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    if not raw.isdigit():
        return None
    return int(raw)
