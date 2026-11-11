"""Мастер брони и переноса. В базу пишет только подтверждение."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.engine import Engine

from dockbot.booking import (
    BookingError,
    FieldError,
    SiteNotConfigured,
    SlotTaken,
    available_dates,
    clean_cargo,
    clean_driver_name,
    clean_make,
    clean_plate,
    create_booking,
    gates_with_slots,
    get_owned,
    load_zone,
    reschedule_booking,
    slots_for_gate,
)
from dockbot.bot.driver import BOOK, driver_menu
from dockbot.bot.ui import (
    ABORT,
    CONFIRM,
    DATE_PREFIX,
    GATE_PREFIX,
    RESTART,
    SLOT_PREFIX,
    actor_ids,
    dates_markup,
    gates_markup,
    reply,
    slots_markup,
    summary_markup,
)
from dockbot.passes import qr_png
from dockbot.present import pass_caption, summary_text

log = logging.getLogger(__name__)
router = Router(name="wizard")
_busy: set[tuple[int, int]] = set()


class BookFlow(StatesGroup):
    date = State()
    gate = State()
    slot = State()
    plate = State()
    make = State()
    driver_name = State()
    cargo = State()
    summary = State()


class MoveFlow(StatesGroup):
    date = State()
    gate = State()
    slot = State()
    summary = State()


@router.callback_query(F.data == BOOK)
async def begin_booking(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_data({"mode": "create"})
    await show_dates(query, state, engine)


async def begin_move(
    query: CallbackQuery, state: FSMContext, engine: Engine, booking_id: int
) -> None:
    await state.set_data({"mode": "move", "booking_id": booking_id})
    await show_dates(query, state, engine)


@router.callback_query(F.data == ABORT)
async def abort(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("mode") == "move":
        text = "Диалог сброшен. Бронь не изменена."
    else:
        text = "Диалог сброшен. Запись не сохранена."
    await reply(query, text, driver_menu())


@router.callback_query(
    StateFilter(BookFlow.date, MoveFlow.date), F.data.startswith(DATE_PREFIX)
)
async def pick_date(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    try:
        day = date.fromisoformat(query.data.removeprefix(DATE_PREFIX))
    except ValueError:
        await query.answer()
        return
    await state.update_data(date=day.isoformat())
    await show_gates(query, state, engine)


@router.callback_query(
    StateFilter(BookFlow.gate, MoveFlow.gate), F.data.startswith(GATE_PREFIX)
)
async def pick_gate(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = query.data.removeprefix(GATE_PREFIX)
    if not raw.isdigit():
        await query.answer()
        return
    await state.update_data(gate_id=int(raw))
    await show_slots(query, state, engine)


@router.callback_query(
    StateFilter(BookFlow.slot, MoveFlow.slot), F.data.startswith(SLOT_PREFIX)
)
async def pick_slot(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = query.data.removeprefix(SLOT_PREFIX)
    if not raw.isdigit():
        await query.answer()
        return
    await state.update_data(slot_ts=int(raw))
    data = await state.get_data()
    if await _slot_from_state(engine, data) is None:
        await show_slots(query, state, engine, preface="Этот слот больше не доступен.")
        return
    if data.get("mode") == "move":
        await show_summary(query, state, engine)
        return
    if all(data.get(key) for key in ("plate", "make", "driver_name", "cargo")):
        await show_summary(query, state, engine)
        return
    await state.set_state(BookFlow.plate)
    await reply(
        query,
        "Госномер транспортного средства.\nСбросить диалог: /cancel",
    )


@router.message(BookFlow.plate, F.text)
async def plate_step(message: Message, state: FSMContext) -> None:
    if await _aborted_text(message, state):
        return
    await _accept_field(message, state, clean_plate, "plate", BookFlow.make, "Марка.")


@router.message(BookFlow.make, F.text)
async def make_step(message: Message, state: FSMContext) -> None:
    if await _aborted_text(message, state):
        return
    await _accept_field(
        message, state, clean_make, "make", BookFlow.driver_name, "ФИО водителя."
    )


@router.message(BookFlow.driver_name, F.text)
async def name_step(message: Message, state: FSMContext) -> None:
    if await _aborted_text(message, state):
        return
    await _accept_field(
        message, state, clean_driver_name, "driver_name", BookFlow.cargo, "Тип груза."
    )


@router.message(BookFlow.cargo, F.text)
async def cargo_step(message: Message, state: FSMContext, engine: Engine) -> None:
    if await _aborted_text(message, state):
        return
    try:
        field = clean_cargo(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    if field.notice:
        await message.answer(f"{field.notice}: {field.value}")
    await state.update_data(cargo=field.value)
    await show_summary(message, state, engine)


@router.callback_query(StateFilter(BookFlow.summary, MoveFlow.summary), F.data == RESTART)
async def restart(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    kept = {"mode": data.get("mode", "create")}
    if kept["mode"] == "move":
        kept["booking_id"] = data.get("booking_id")
    await state.set_data(kept)
    await show_dates(query, state, engine)


@router.callback_query(StateFilter(BookFlow.summary, MoveFlow.summary), F.data == CONFIRM)
async def confirm(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    ids = actor_ids(query)
    if ids is None or ids in _busy:
        await query.answer()
        return
    _busy.add(ids)
    try:
        data = await state.get_data()
        telegram_id, chat_id = ids
        moving = data.get("mode") == "move"
        slot = await _slot_from_state(engine, data)
        if slot is None:
            await show_slots(
                query, state, engine, preface="Этот слот больше не доступен."
            )
            return
        if moving and not data.get("booking_id"):
            await state.clear()
            await reply(query, "Сводка устарела. Начните заново.", driver_menu())
            return
        if not moving and not all(
            data.get(key) for key in ("plate", "make", "driver_name", "cargo")
        ):
            await state.clear()
            await reply(query, "Сводка устарела. Начните заново.", driver_menu())
            return
        try:
            if moving:
                trip = await asyncio.to_thread(
                    reschedule_booking,
                    engine,
                    telegram_id=telegram_id,
                    chat_id=chat_id,
                    booking_id=int(data["booking_id"]),
                    gate_id=slot.gate_id,
                    slot_start=slot.start,
                )
            else:
                trip = await asyncio.to_thread(
                    create_booking,
                    engine,
                    gate_id=slot.gate_id,
                    slot_start=slot.start,
                    plate=data["plate"],
                    make=data["make"],
                    driver_name=data["driver_name"],
                    cargo_type=data["cargo"],
                    telegram_id=telegram_id,
                    chat_id=chat_id,
                )
        except SlotTaken as exc:
            await show_slots(query, state, engine, preface=str(exc))
            return
        except BookingError as exc:
            await reply(query, str(exc))
            return
        await state.clear()
        zone = await asyncio.to_thread(load_zone, engine)
        await _send_pass(
            query,
            trip.pass_token,
            pass_caption(trip, zone),
            trip.id,
            preface="Бронь перенесена." if moving else None,
        )
    finally:
        _busy.discard(ids)


async def show_dates(
    event: Message | CallbackQuery,
    state: FSMContext,
    engine: Engine,
    preface: str | None = None,
) -> None:
    data = await state.get_data()
    moving = data.get("mode") == "move"
    ignore = int(data["booking_id"]) if moving and data.get("booking_id") else None
    try:
        dates = await asyncio.to_thread(
            available_dates, engine, ignore_booking_id=ignore
        )
    except SiteNotConfigured as exc:
        await state.clear()
        await reply(event, str(exc), driver_menu())
        return
    await state.set_state(MoveFlow.date if moving else BookFlow.date)
    text = "Выберите дату." if dates else "На ближайшие дни свободных слотов нет."
    if preface:
        text = f"{preface}\n{text}"
    await reply(event, text, dates_markup(dates))


async def show_gates(
    event: Message | CallbackQuery,
    state: FSMContext,
    engine: Engine,
    preface: str | None = None,
) -> None:
    data = await state.get_data()
    moving = data.get("mode") == "move"
    ignore = int(data["booking_id"]) if moving and data.get("booking_id") else None
    day = date.fromisoformat(data["date"])
    try:
        gates = await asyncio.to_thread(
            gates_with_slots, engine, day, ignore_booking_id=ignore
        )
    except SiteNotConfigured as exc:
        await state.clear()
        await reply(event, str(exc), driver_menu())
        return
    await state.set_state(MoveFlow.gate if moving else BookFlow.gate)
    if not gates:
        await show_dates(
            event, state, engine, preface="На эту дату свободных ворот нет."
        )
        return
    text = "Выберите ворота."
    if preface:
        text = f"{preface}\n{text}"
    await reply(event, text, gates_markup(gates))


async def show_slots(
    event: Message | CallbackQuery,
    state: FSMContext,
    engine: Engine,
    preface: str | None = None,
) -> None:
    data = await state.get_data()
    slot = await _slot_choices(engine, data)
    if slot is None:
        await show_gates(event, state, engine, preface=preface)
        return
    choices, zone = slot
    if not choices:
        await show_gates(
            event,
            state,
            engine,
            preface=preface or "На эти ворота свободных слотов нет.",
        )
        return
    moving = data.get("mode") == "move"
    await state.set_state(MoveFlow.slot if moving else BookFlow.slot)
    text = "Выберите время."
    if preface:
        text = f"{preface}\n{text}"
    await reply(event, text, slots_markup(choices, zone))


async def show_summary(
    event: Message | CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    data = await state.get_data()
    found = await _slot_choices(engine, data)
    if found is None:
        await show_dates(event, state, engine)
        return
    choices, zone = found
    slot_ts = data.get("slot_ts")
    selected = next(
        (slot for slot in choices if int(slot.start.timestamp()) == slot_ts),
        None,
    )
    if selected is None:
        await show_slots(event, state, engine, preface="Этот слот больше не доступен.")
        return
    moving = data.get("mode") == "move"
    if moving:
        ids = actor_ids(event)
        if ids is None:
            await reply(event, "Бронь не найдена.")
            return
        try:
            trip = await asyncio.to_thread(
                get_owned, engine, ids[0], ids[1], int(data["booking_id"])
            )
        except BookingError as exc:
            await state.clear()
            await reply(event, str(exc), driver_menu())
            return
        plate, make, driver_name, cargo = (
            trip.plate,
            trip.make,
            trip.driver_name,
            trip.cargo_type,
        )
        await state.set_state(MoveFlow.summary)
    else:
        plate = data.get("plate", "")
        make = data.get("make", "")
        driver_name = data.get("driver_name", "")
        cargo = data.get("cargo", "")
        await state.set_state(BookFlow.summary)
    await reply(
        event,
        summary_text(
            start=selected.start,
            end=selected.end,
            gate_code=selected.gate_code,
            plate=plate,
            make=make,
            driver_name=driver_name,
            cargo_type=cargo,
            zone=zone,
            moving=moving,
        ),
        summary_markup(moving),
    )


async def _slot_from_state(engine: Engine, data: dict):
    found = await _slot_choices(engine, data)
    if found is None:
        return None
    choices, _zone = found
    slot_ts = data.get("slot_ts")
    return next(
        (slot for slot in choices if int(slot.start.timestamp()) == slot_ts),
        None,
    )


async def _slot_choices(engine: Engine, data: dict):
    if "date" not in data or "gate_id" not in data:
        return None
    moving = data.get("mode") == "move"
    ignore = int(data["booking_id"]) if moving and data.get("booking_id") else None
    day = date.fromisoformat(data["date"])
    try:
        choices = await asyncio.to_thread(
            slots_for_gate,
            engine,
            day,
            int(data["gate_id"]),
            ignore_booking_id=ignore,
        )
        zone = await asyncio.to_thread(load_zone, engine)
    except SiteNotConfigured:
        return None
    return choices, zone


async def _accept_field(message, state, cleaner, key, nxt, question: str) -> None:
    try:
        field = cleaner(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    if field.notice:
        await message.answer(f"{field.notice}: {field.value}")
    await state.update_data(**{key: field.value})
    await state.set_state(nxt)
    await message.answer(question)


async def _aborted_text(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip().casefold() != "отмена":
        return False
    data = await state.get_data()
    await state.clear()
    if data.get("mode") == "move":
        text = "Диалог сброшен. Бронь не изменена."
    else:
        text = "Диалог сброшен. Запись не сохранена."
    await message.answer(text, reply_markup=driver_menu())
    return True


async def _send_pass(
    query: CallbackQuery,
    token: str,
    caption: str,
    booking_id: int,
    preface: str | None,
) -> None:
    from aiogram.types import BufferedInputFile

    await query.answer()
    message = query.message
    if message is None or not hasattr(message, "answer_photo"):
        return
    try:
        if preface:
            await message.answer(preface)
        await message.answer_photo(
            BufferedInputFile(qr_png(token), filename="pass.png"),
            caption=caption,
        )
    except Exception:
        log.warning("pass image was not delivered for booking %s", booking_id)
        await message.answer(
            "Запись сохранена. Откройте «Мои брони» и нажмите «Показать пропуск»."
        )
