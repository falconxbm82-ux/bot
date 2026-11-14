"""Будущие брони этого чата."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy.engine import Engine

from dockbot.booking import (
    BookingNotFound,
    NotEditable,
    SiteNotConfigured,
    WindowClosed,
    cancel_booking,
    change_block,
    get_owned,
    list_future,
    load_zone,
)
from dockbot.bot.driver import MY_BOOKINGS, driver_menu
from dockbot.bot.ui import (
    CANCEL_PREFIX,
    CANCEL_YES_PREFIX,
    MOVE_PREFIX,
    OPEN_PREFIX,
    PAGE_PREFIX,
    PASS_PREFIX,
    actor_ids,
    cancel_markup,
    card_markup,
    reply,
    trips_markup,
)
from dockbot.bot.wizard import begin_move
from dockbot.passes import qr_png
from dockbot.present import card_text, local_parts, pass_caption

log = logging.getLogger(__name__)
router = Router(name="mine")


@router.callback_query(F.data == MY_BOOKINGS)
async def my_bookings(query: CallbackQuery, engine: Engine) -> None:
    await _show_list(query, engine, 0)


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
        await reply(query, card_text(trip, zone, block), card_markup(trip.id, False))
        return
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    await reply(
        query,
        f"Отменить бронь на {date_text} {clock}, ворота №{trip.gate_code}?",
        cancel_markup(trip.id),
    )


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
    await reply(query, "Бронь отменена.", driver_menu())


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
        await reply(query, card_text(trip, zone, block), card_markup(trip.id, False))
        return
    await begin_move(query, state, engine, booking_id)


async def _show_list(query: CallbackQuery, engine: Engine, offset: int) -> None:
    ids = actor_ids(query)
    if ids is None:
        await query.answer()
        return
    try:
        trips = await asyncio.to_thread(list_future, engine, ids[0], ids[1])
        zone = await asyncio.to_thread(load_zone, engine)
    except SiteNotConfigured as exc:
        await reply(query, str(exc), driver_menu())
        return
    if not trips:
        await reply(query, "Будущих броней нет.", driver_menu())
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
    await reply(query, card_text(trip, zone, block), card_markup(trip.id, block is None))


def _id(data: str, prefix: str) -> int | None:
    raw = data.removeprefix(prefix)
    if not raw.isdigit():
        return None
    return int(raw)
