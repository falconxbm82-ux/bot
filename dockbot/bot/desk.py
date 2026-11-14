"""Запись рейса администратором, если у водителя нет чата."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.engine import Engine

from dockbot.access import AccessDenied, active_staff
from dockbot.booking import (
    FieldError,
    SiteNotConfigured,
    SlotTaken,
    available_dates,
    cargo_norms,
    clean_cargo,
    clean_driver_name,
    clean_make,
    create_booking,
    gates_with_slots,
    load_zone,
    normalize_plate,
    slots_for_gate,
)
from dockbot.bot.keyboard import keyboard_for
from dockbot.bot.ui import (
    ABORT,
    CARGO_PREFIX,
    DATE_PREFIX,
    GATE_PREFIX,
    SLOT_PREFIX,
    actor_ids,
    cargo_markup,
    dates_markup,
    gates_markup,
    menu_markup,
    reply,
    slots_markup,
    urgent_markup,
    vehicle_markup,
)
from dockbot.models import StaffRole
from dockbot.passes import qr_png
from dockbot.present import pass_caption

router = Router(name="desk")
WIZARD_DAYS = 7
_CARGO = {
    "pallets": "Паллеты",
    "bulk": "Навал",
    "liquid": "Жидкость",
    "reefer": "Рефрижератор",
}
_NOTE = "Напоминания не уйдут: у рейса нет чата водителя. Пропуск покажет охрана на КПП."


class DeskFlow(StatesGroup):
    vehicle = State()
    cargo = State()
    cargo_note = State()
    urgent = State()
    date = State()
    gate = State()
    slot = State()
    plate = State()
    make = State()
    driver_name = State()


@router.callback_query(F.data == "ad:desk")
async def begin_desk(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    await state.clear()
    await state.set_state(DeskFlow.vehicle)
    await reply(query, "Запись без чата водителя.\nВид транспорта.", vehicle_markup())


@router.callback_query(DeskFlow.vehicle, F.data.startswith("bk:veh:"))
async def desk_vehicle(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    kind = (query.data or "").removeprefix("bk:veh:")
    if kind not in {"light", "heavy"}:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(vehicle=kind)
    norms = {code: minutes for code, _label, minutes in await asyncio.to_thread(cargo_norms, engine)}
    await state.set_state(DeskFlow.cargo)
    await reply(query, "Тип груза. Длина окна — у кнопки.", cargo_markup(norms))


@router.callback_query(DeskFlow.cargo, F.data.startswith(CARGO_PREFIX))
async def desk_cargo(query: CallbackQuery, state: FSMContext) -> None:
    code = (query.data or "").removeprefix(CARGO_PREFIX)
    if code == "other":
        await state.set_state(DeskFlow.cargo_note)
        await reply(query, "Напишите тип груза.", menu_markup())
        return
    label = _CARGO.get(code)
    if label is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(cargo=label)
    await state.set_state(DeskFlow.urgent)
    await reply(query, "Срочность рейса.", urgent_markup())


@router.message(DeskFlow.cargo_note, F.text)
async def desk_cargo_note(message: Message, state: FSMContext) -> None:
    try:
        field = clean_cargo(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(cargo=field.value)
    await state.set_state(DeskFlow.urgent)
    await message.answer("Срочность рейса.", reply_markup=urgent_markup())


@router.callback_query(DeskFlow.urgent, F.data.startswith("bk:urgent:"))
async def desk_urgent(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix("bk:urgent:")
    if raw not in {"0", "1"}:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(urgent=raw == "1")
    await _dates(query, state, engine)


@router.callback_query(DeskFlow.date, F.data.startswith(DATE_PREFIX))
async def desk_date(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix(DATE_PREFIX)
    try:
        date.fromisoformat(raw)
    except ValueError:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(date=raw)
    await _gates(query, state, engine)


@router.callback_query(DeskFlow.gate, F.data.startswith(GATE_PREFIX))
async def desk_gate(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix(GATE_PREFIX)
    if not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(gate_id=int(raw))
    await _slots(query, state, engine)


@router.callback_query(DeskFlow.slot, F.data.startswith(SLOT_PREFIX))
async def desk_slot(query: CallbackQuery, state: FSMContext) -> None:
    raw = (query.data or "").removeprefix(SLOT_PREFIX)
    if not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(slot_ts=int(raw))
    await state.set_state(DeskFlow.plate)
    await reply(query, "Госномер, например А123ВС178.", menu_markup())


@router.message(DeskFlow.plate, F.text)
async def desk_plate(message: Message, state: FSMContext) -> None:
    try:
        plate = normalize_plate(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(plate=plate)
    await state.set_state(DeskFlow.make)
    await message.answer("Марка.")


@router.message(DeskFlow.make, F.text)
async def desk_make(message: Message, state: FSMContext) -> None:
    try:
        field = clean_make(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(make=field.value)
    await state.set_state(DeskFlow.driver_name)
    await message.answer("ФИО водителя.")


@router.message(DeskFlow.driver_name, F.text)
async def desk_name(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        field = clean_driver_name(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    slot = await _selected(engine, data)
    if slot is None:
        await state.clear()
        await message.answer(
            "Слот уже недоступен. Откройте «Сводка дня» и нажмите «Записать без чата».",
            reply_markup=keyboard_for(StaffRole.admin),
        )
        return
    try:
        trip = await asyncio.to_thread(
            create_booking,
            engine,
            gate_id=slot.gate_id,
            slot_start=slot.start,
            plate=data.get("plate", ""),
            make=data.get("make", ""),
            driver_name=field.value,
            cargo_type=data.get("cargo", ""),
            telegram_id=None,
            chat_id=None,
            vehicle=data.get("vehicle") or "heavy",
            urgent=bool(data.get("urgent")),
        )
    except SlotTaken as exc:
        await message.answer(str(exc))
        await _slots(message, state, engine)
        return
    except SiteNotConfigured as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=keyboard_for(StaffRole.admin))
        return
    await state.clear()
    zone = await asyncio.to_thread(load_zone, engine)
    await message.answer_photo(
        BufferedInputFile(qr_png(trip.pass_token), filename="pass.png"),
        caption=pass_caption(trip, zone) + "\n" + _NOTE,
    )
    await message.answer(_NOTE, reply_markup=keyboard_for(StaffRole.admin))


@router.callback_query(DeskFlow.date, F.data == ABORT)
@router.callback_query(DeskFlow.vehicle, F.data == ABORT)
async def desk_abort(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await reply(query, "Запись без чата отменена.", keyboard_for(StaffRole.admin))


async def _dates(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    try:
        days = await asyncio.to_thread(
            available_dates,
            engine,
            cargo=data.get("cargo"),
            vehicle=data.get("vehicle"),
        )
    except SiteNotConfigured as exc:
        await state.clear()
        await reply(event, str(exc), keyboard_for(StaffRole.admin))
        return
    days = days[:WIZARD_DAYS]
    await state.set_state(DeskFlow.date)
    if not days:
        await reply(
            event,
            "Нет свободных окон. Откройте «Слоты» и нажмите «Сгенерировать 08:00–20:00».",
            menu_markup(),
        )
        return
    await reply(event, "Выберите дату.", dates_markup(days))


async def _gates(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    day = date.fromisoformat(data["date"])
    gates = await asyncio.to_thread(
        gates_with_slots,
        engine,
        day,
        cargo=data.get("cargo"),
        vehicle=data.get("vehicle"),
    )
    await state.set_state(DeskFlow.gate)
    if not gates:
        await reply(event, "Свободных ворот нет. Вернитесь к дате.", menu_markup())
        return
    await reply(event, "Выберите ворота.", gates_markup(gates))


async def _slots(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    choices, zone = await _choices(engine, data)
    await state.set_state(DeskFlow.slot)
    if not choices:
        await reply(event, "Свободных окон нет. Выберите другие ворота.", menu_markup())
        return
    await reply(event, "Выберите окно.", slots_markup(choices, zone))


async def _selected(engine: Engine, data: dict):
    choices, _zone = await _choices(engine, data)
    stamp = data.get("slot_ts")
    return next((slot for slot in choices if int(slot.start.timestamp()) == stamp), None)


async def _choices(engine: Engine, data: dict):
    day = date.fromisoformat(data["date"])
    choices = await asyncio.to_thread(
        slots_for_gate,
        engine,
        day,
        int(data["gate_id"]),
        cargo=data.get("cargo"),
        vehicle=data.get("vehicle"),
    )
    zone = await asyncio.to_thread(load_zone, engine)
    return choices, zone


async def _admin(query: CallbackQuery, engine: Engine) -> bool:
    user = query.from_user
    if user is None:
        await query.answer()
        return False
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.admin:
        await query.answer("Недостаточно прав.", show_alert=True)
        return False
    return True
