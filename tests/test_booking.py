"""Бронь: слот, пропуск, отмена, перенос и чужая запись."""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    env_file = Path(__file__).resolve().parents[1] / ".env"
    url = ""
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
    if "/" not in url:
        raise RuntimeError("DATABASE_URL is missing")
    return url.rsplit("/", 1)[0] + "/dockbot_test"


os.environ["DATABASE_URL"] = _test_database_url()

from dockbot.booking import (  # noqa: E402
    DAY_TAKEN,
    BookingError,
    FieldError,
    NotEditable,
    SlotTaken,
    WindowClosed,
    BookingNotFound,
    available_dates,
    cancel_booking,
    change_block,
    clean_driver_name,
    clean_make,
    clean_plate,
    clean_weight_tons,
    create_booking,
    get_owned,
    list_future,
    normalize_plate,
    reschedule_booking,
    slots_for_gate,
)
from dockbot.db import make_engine, upgrade_head  # noqa: E402
from dockbot.models import (  # noqa: E402
    PLATE_MAX,
    OCCUPYING_STATUSES,
    Booking,
    BookingStatus,
    ClosedSlot,
    BookingStatusEvent,
    Gate,
    GateHour,
    NotificationLog,
    SiteSettings,
    StatusSource,
)
from dockbot.passes import qr_png  # noqa: E402

ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
TODAY = NOW.astimezone(ZONE).date()
TOMORROW = TODAY + timedelta(days=1)
ENGINE = make_engine(os.environ["DATABASE_URL"])


def setUpModule() -> None:
    upgrade_head()


def tearDownModule() -> None:
    ENGINE.dispose()


class BookingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._wipe()
        with Session(ENGINE) as session:
            with session.begin():
                session.add(
                    SiteSettings(
                        id=1,
                        time_zone="Europe/Moscow",
                        latitude=None,
                        longitude=None,
                        cancel_window_hours=2,
                    )
                )
                first = Gate(code="1", name="Ворота 1")
                second = Gate(code="2", name="Ворота 2")
                session.add_all([first, second])
                session.flush()
                self.gate_a = first.id
                self.gate_b = second.id
                for gate_id in (first.id, second.id):
                    for weekday in range(1, 8):
                        session.add(
                            GateHour(
                                gate_id=gate_id,
                                weekday=weekday,
                                start_time=time(8, 0),
                                end_time=time(12, 0),
                                slot_minutes=60,
                            )
                        )

    def tearDown(self) -> None:
        self._wipe()

    def test_plate_is_trimmed_and_uppercased(self) -> None:
        field = clean_plate("  ab 123  ")
        self.assertEqual(field.value, "AB 123")
        self.assertIsNone(field.notice)
        with self.assertRaises(FieldError):
            clean_plate("   ")
        long = clean_plate("x" * (PLATE_MAX + 5))
        self.assertEqual(len(long.value), PLATE_MAX)
        self.assertIsNotNone(long.notice)

    def test_browsing_does_not_create_a_booking(self) -> None:
        dates = available_dates(ENGINE, now=NOW)
        self.assertIn(TODAY, dates)
        self.assertIn(TOMORROW, dates)
        slots = slots_for_gate(ENGINE, TODAY, self.gate_a, now=NOW)
        hours = [slot.start.astimezone(ZONE).hour for slot in slots]
        self.assertEqual(hours, [10, 11])
        self.assertEqual(self._count(), 0)

    def test_future_slot_is_booked_and_near_slot_is_confirmed(self) -> None:
        later = self._book(TOMORROW, 8)
        self.assertEqual(later.status, BookingStatus.booked)
        self.assertEqual(len(later.pass_pin), 6)
        self.assertTrue(later.pass_pin.isdigit())
        soon = self._book(TODAY, 10)
        self.assertEqual(soon.status, BookingStatus.confirmed)
        with Session(ENGINE) as session:
            event = session.scalar(
                select(BookingStatusEvent).where(
                    BookingStatusEvent.booking_id == later.id
                )
            )
            self.assertIsNotNone(event)
            assert event is not None
            self.assertIsNone(event.from_status)
            self.assertEqual(event.to_status, BookingStatus.booked)
            self.assertEqual(event.source, StatusSource.driver)

    def test_same_plate_or_driver_cannot_book_the_same_day_twice(self) -> None:
        from dockbot.booking import pair_booked_on
        from dockbot.roster import NOT_LISTED

        first = self._book(TOMORROW, 8, plate="a100aa77")
        self.assertEqual(first.plate, "А100АА77")
        self.assertTrue(
            pair_booked_on(ENGINE, TOMORROW, "А100АА77", "Иванов Иван Иванович")
        )
        with self.assertRaises(BookingError) as same_pair:
            self._book(TOMORROW, 9)
        self.assertEqual(str(same_pair.exception), DAY_TAKEN)
        with self.assertRaises(BookingError) as same_chat:
            self._book(
                TOMORROW,
                9,
                plate="В200ВВ77",
                telegram_id=1,
                chat_id=1,
                driver_name="Петров Петр Петрович",
            )
        self.assertEqual(str(same_chat.exception), DAY_TAKEN)
        with self.assertRaises(BookingError) as unknown:
            self._book(
                TOMORROW,
                9,
                plate="К456КК150",
                telegram_id=9,
                chat_id=9,
                driver_name="Петров Петр Петрович",
            )
        self.assertEqual(str(unknown.exception), NOT_LISTED)
        cancel_booking(ENGINE, telegram_id=1, chat_id=1, booking_id=first.id, now=NOW)
        again = self._book(TOMORROW, 9, plate="А100АА77")
        self.assertNotEqual(again.id, first.id)
        other_day = self._book(TODAY, 10, plate="А100АА77", telegram_id=8, chat_id=8)
        self.assertNotEqual(other_day.id, again.id)

    def test_driver_fields_say_what_to_fix(self) -> None:
        self.assertEqual(
            clean_driver_name("  Иванов   Иван   Иванович ").value,
            "Иванов Иван Иванович",
        )
        for bad in ("Иванов Иван", "Иванов Иван 3", "Иванов Иван Иванович лишний"):
            with self.assertRaises(FieldError) as caught:
                clean_driver_name(bad)
            self.assertIn("три слова", str(caught.exception))
        with self.assertRaises(FieldError) as plate:
            normalize_plate("A1")
        self.assertIn("А123ВС178", str(plate.exception))
        with self.assertRaises(FieldError) as make:
            clean_make("   ")
        self.assertIn("пуст", str(make.exception))
        for bad_weight in ("нет", "0", "41", "1.5"):
            with self.assertRaises(FieldError) as weight:
                clean_weight_tons(bad_weight)
            self.assertIn("1 до 40", str(weight.exception))
        self.assertEqual(clean_weight_tons("6 т"), 6000)
        with self.assertRaises(FieldError) as light_weight:
            clean_weight_tons("6", vehicle="light")
        self.assertIn("1 до 5", str(light_weight.exception))
        self.assertEqual(clean_weight_tons("3", vehicle="light"), 3000)

    def test_occupied_window_hides_until_cancel_and_two_confirms_stay_one(self) -> None:
        from dockbot.present import card_text

        start = self._slot(TOMORROW, self.gate_a, 8).start
        trip = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=start,
            plate="А100АА77",
            make="КАМАЗ",
            driver_name="Иванов Иван Иванович",
            cargo_type="Паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
            weight_kg=6000,
        )
        self.assertEqual(trip.unload_total, 75)
        self.assertEqual(trip.slot_end - trip.slot_start, timedelta(minutes=75))
        self.assertIn(
            "Разгрузка: база 60 мин, масса +15 мин, итого 75 мин, потолок 240",
            card_text(trip, ZONE, None),
        )
        hidden = [
            item.start
            for item in slots_for_gate(
                ENGINE, TOMORROW, self.gate_a, now=NOW, cargo="Паллеты", weight_kg=1000
            )
        ]
        self.assertNotIn(start, hidden)
        cancel_booking(ENGINE, telegram_id=1, chat_id=1, booking_id=trip.id, now=NOW)
        shown = [
            item.start
            for item in slots_for_gate(
                ENGINE, TOMORROW, self.gate_a, now=NOW, cargo="Паллеты", weight_kg=1000
            )
        ]
        self.assertIn(start, shown)
        first = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=start,
            plate="А100АА77",
            make="КАМАЗ",
            driver_name="Иванов Иван Иванович",
            cargo_type="Паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
            weight_kg=6000,
        )
        second = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=start,
            plate="А100АА77",
            make="КАМАЗ",
            driver_name="Иванов Иван Иванович",
            cargo_type="Паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
            weight_kg=6000,
        )
        self.assertEqual(first.id, second.id)
        with self.assertRaises(SlotTaken):
            create_booking(
                ENGINE,
                gate_id=self.gate_a,
                slot_start=start,
                plate="В200ВВ77",
                make="ГАЗ",
                driver_name="Петров Петр Петрович",
                cargo_type="Паллеты",
                telegram_id=2,
                chat_id=2,
                now=NOW,
                weight_kg=1000,
            )

    def test_failed_confirm_leaves_no_booking_and_no_event(self) -> None:
        from dockbot import booking as booking_module

        slot = self._slot(TOMORROW, self.gate_a, 8)
        real = booking_module._insert_booking

        def fail_after_insert(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("confirm failed")

        with patch.object(booking_module, "_insert_booking", fail_after_insert):
            with self.assertRaises(RuntimeError):
                create_booking(
                    ENGINE,
                    gate_id=self.gate_a,
                    slot_start=slot.start,
                    plate="A100AA77",
                    make="КАМАЗ",
                    driver_name="Иванов Иван Иванович",
                    cargo_type="Паллеты",
                    telegram_id=1,
                    chat_id=1,
                    now=NOW,
                )
        self.assertEqual(self._count(), 0)
        with Session(ENGINE) as session:
            events = session.scalar(
                select(func.count()).select_from(BookingStatusEvent)
            )
        self.assertEqual(events, 0)

    def test_double_confirm_by_same_driver_is_one_booking(self) -> None:
        slot = self._slot(TOMORROW, self.gate_a, 8)
        first = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=slot.start,
            plate="А100АА77",
            make="Volvo",
            driver_name="Иванов Иван Иванович",
            cargo_type="паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
        )
        second = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=slot.start,
            plate="А100АА77",
            make="Volvo",
            driver_name="Иванов Иван Иванович",
            cargo_type="паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self._occupying(self.gate_a, slot.start), 1)

    def test_concurrent_confirms_of_one_slot_yield_one_booking(self) -> None:
        slot = self._slot(TOMORROW, self.gate_a, 8)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def attempt(telegram_id: int) -> str:
            barrier.wait()
            try:
                create_booking(
                    ENGINE,
                    gate_id=self.gate_a,
                    slot_start=slot.start,
                    plate="А100АА77" if telegram_id == 11 else "В200ВВ77",
                    make="Volvo",
                    driver_name=(
                        "Иванов Иван Иванович"
                        if telegram_id == 11
                        else "Петров Петр Петрович"
                    ),
                    cargo_type="груз",
                    telegram_id=telegram_id,
                    chat_id=telegram_id,
                    now=NOW,
                )
                return "ok"
            except SlotTaken:
                return "taken"

        threads = []
        for telegram_id in (11, 12):
            thread = threading.Thread(target=lambda tid=telegram_id: results.append(attempt(tid)))
            threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["ok", "taken"])
        self.assertEqual(self._occupying(self.gate_a, slot.start), 1)

    def test_different_gates_at_the_same_time_both_succeed(self) -> None:
        barrier = threading.Barrier(2)
        created: list[int] = []

        def attempt(gate_id: int, telegram_id: int) -> None:
            slot = self._slot(TOMORROW, gate_id, 8)
            barrier.wait()
            trip = create_booking(
                ENGINE,
                gate_id=gate_id,
                slot_start=slot.start,
                plate="С300СС77" if telegram_id == 21 else "В022ВВ77",
                make="MAN",
                driver_name=(
                    "Сидоров Сидор Сидорович"
                    if telegram_id == 21
                    else "Алексеев Алексей Алексеевич"
                ),
                cargo_type="груз",
                telegram_id=telegram_id,
                chat_id=telegram_id,
                now=NOW,
            )
            created.append(trip.id)

        threads = [
            threading.Thread(target=attempt, args=(self.gate_a, 21)),
            threading.Thread(target=attempt, args=(self.gate_b, 22)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0], created[1])

    def test_late_status_frees_the_slot_and_arrived_does_not(self) -> None:
        slot = self._slot(TOMORROW, self.gate_a, 8)
        trip = self._book(TOMORROW, 8, slot_start=slot.start)
        self._set_status(trip.id, BookingStatus.arrived)
        with self.assertRaises(SlotTaken):
            self._book(TOMORROW, 8, telegram_id=9, chat_id=9, slot_start=slot.start)
        self._set_status(trip.id, BookingStatus.late)
        freed = [
            item.start.astimezone(ZONE).hour
            for item in slots_for_gate(ENGINE, TOMORROW, self.gate_a, now=NOW)
        ]
        self.assertIn(8, freed)
        again = self._book(
            TOMORROW,
            8,
            telegram_id=9,
            chat_id=9,
            slot_start=slot.start,
            plate="В200ВВ77",
            driver_name="Петров Петр Петрович",
        )
        self.assertNotEqual(again.id, trip.id)
        self.assertEqual(self._occupying(self.gate_a, slot.start), 1)

    def test_cancel_inside_window_frees_slot_and_outside_window_rejects(self) -> None:
        slot = self._slot(TOMORROW, self.gate_a, 8)
        trip = self._book(TOMORROW, 8)
        self.assertIsNone(change_block(ENGINE, trip, now=NOW))
        cancel_booking(
            ENGINE, telegram_id=1, chat_id=1, booking_id=trip.id, now=NOW
        )
        self.assertEqual(self._status(trip.id), BookingStatus.cancelled)
        self.assertEqual(self._occupying(self.gate_a, slot.start), 0)
        taken = self._book(TOMORROW, 8, telegram_id=3, chat_id=3)
        self.assertNotEqual(taken.id, trip.id)

        soon = self._book(TODAY, 10, telegram_id=4, chat_id=4)
        self.assertEqual(
            change_block(ENGINE, soon, now=NOW),
            "Отмена и перенос уже закрыты: до слота меньше 2 ч. Откройте «Мои брони».",
        )
        with self.assertRaises(WindowClosed):
            cancel_booking(
                ENGINE, telegram_id=4, chat_id=4, booking_id=soon.id, now=NOW
            )
        self.assertEqual(self._status(soon.id), BookingStatus.confirmed)

    def test_reschedule_keeps_the_pass_and_rejects_a_taken_slot(self) -> None:
        original = self._book(TOMORROW, 8)
        target = self._slot(TOMORROW, self.gate_a, 10)
        moved = reschedule_booking(
            ENGINE,
            telegram_id=1,
            chat_id=1,
            booking_id=original.id,
            gate_id=self.gate_a,
            slot_start=target.start,
            now=NOW,
        )
        self.assertEqual(moved.id, original.id)
        self.assertEqual(moved.pass_token, original.pass_token)
        self.assertEqual(moved.pass_pin, original.pass_pin)
        self.assertEqual(int(moved.slot_start.timestamp()), int(target.start.timestamp()))
        other = self._book(
            TOMORROW,
            8,
            telegram_id=5,
            chat_id=5,
            plate="В200ВВ77",
            driver_name="Петров Петр Петрович",
        )
        held = self._start(other.id)
        with self.assertRaises(SlotTaken):
            reschedule_booking(
                ENGINE,
                telegram_id=5,
                chat_id=5,
                booking_id=other.id,
                gate_id=self.gate_a,
                slot_start=target.start,
                now=NOW,
            )
        self.assertEqual(self._status(other.id), BookingStatus.booked)
        self.assertEqual(self._start(other.id), held)

    def test_reschedule_onto_the_same_slot_is_not_a_conflict(self) -> None:
        slot = self._slot(TOMORROW, self.gate_a, 9)
        trip = self._book(TOMORROW, 9)
        again = reschedule_booking(
            ENGINE,
            telegram_id=1,
            chat_id=1,
            booking_id=trip.id,
            gate_id=self.gate_a,
            slot_start=slot.start,
            now=NOW,
        )
        self.assertEqual(again.id, trip.id)
        self.assertEqual(self._occupying(self.gate_a, slot.start), 1)

    def test_someone_elses_booking_is_hidden_and_not_editable(self) -> None:
        trip = self._book(TOMORROW, 11, telegram_id=1, chat_id=1)
        self.assertEqual(list_future(ENGINE, 1, 1, now=NOW)[0].id, trip.id)
        self.assertEqual(list_future(ENGINE, 2, 1, now=NOW), [])
        self.assertEqual(list_future(ENGINE, 1, 2, now=NOW), [])
        with self.assertRaises(BookingNotFound):
            get_owned(ENGINE, 2, 1, trip.id)
        with self.assertRaises(BookingNotFound):
            cancel_booking(
                ENGINE, telegram_id=2, chat_id=1, booking_id=trip.id, now=NOW
            )
        with self.assertRaises(BookingNotFound):
            reschedule_booking(
                ENGINE,
                telegram_id=2,
                chat_id=1,
                booking_id=trip.id,
                gate_id=self.gate_b,
                slot_start=self._slot(TOMORROW, self.gate_b, 8).start,
                now=NOW,
            )
        self.assertEqual(self._status(trip.id), BookingStatus.booked)

    def test_arrived_booking_cannot_be_cancelled(self) -> None:
        trip = self._book(TOMORROW, 11)
        self._set_status(trip.id, BookingStatus.arrived)
        fresh = get_owned(ENGINE, 1, 1, trip.id)
        self.assertEqual(
            change_block(ENGINE, fresh, now=NOW),
            "Эту бронь уже нельзя изменить. Откройте «Мои брони».",
        )
        with self.assertRaises(NotEditable):
            cancel_booking(
                ENGINE, telegram_id=1, chat_id=1, booking_id=trip.id, now=NOW
            )

    def test_qr_png_depends_on_the_token(self) -> None:
        first = qr_png("alpha-token")
        second = qr_png("beta-token")
        self.assertTrue(first.startswith(b"\x89PNG"))
        self.assertNotEqual(first, second)

    def test_roster_is_ten_drivers_and_light_truck_is_shorter(self) -> None:
        from dockbot.booking import (
            known_driver_rows,
            offered_cargos,
            unload_minutes,
        )
        from dockbot.present import card_text
        from dockbot.roster import NOT_LISTED, STAND_DRIVERS

        rows = known_driver_rows(ENGINE)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(STAND_DRIVERS), 10)
        self.assertEqual(set(rows), set(STAND_DRIVERS))
        heavy = unload_minutes(60, 6000, 5000)
        light = unload_minutes(60, 6000, 5000, "light")
        self.assertEqual(heavy, 75)
        self.assertLess(light, heavy)
        light_cargo = [label for _code, label, _minutes in offered_cargos(ENGINE, "light")]
        heavy_cargo = [label for _code, label, _minutes in offered_cargos(ENGINE, "heavy")]
        self.assertIn("Коробки", light_cargo)
        self.assertNotIn("Паллеты", light_cargo)
        self.assertIn("Паллеты", heavy_cargo)
        self.assertNotIn("Коробки", heavy_cargo)
        boxes = next(
            minutes
            for _code, label, minutes in offered_cargos(ENGINE, "light")
            if label == "Коробки"
        )
        self.assertLess(boxes, 30)
        start = self._slot(TOMORROW, self.gate_a, 8).start
        trip = create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=start,
            plate="А100АА77",
            make="ГАЗ",
            driver_name="Иванов Иван Иванович",
            cargo_type="Паллеты",
            telegram_id=1,
            chat_id=1,
            now=NOW,
            vehicle="light",
            weight_kg=1000,
        )
        self.assertEqual(trip.vehicle_kind, "light")
        self.assertLess(trip.slot_end - trip.slot_start, timedelta(minutes=60))
        card = card_text(trip, ZONE, None)
        self.assertIn("Транспорт: малотоннажный", card)
        self.assertIn("малотоннажный минус", card)
        self.assertIn("потолок 120", card)
        with self.assertRaises(FieldError):
            create_booking(
                ENGINE,
                gate_id=self.gate_a,
                slot_start=self._slot(TOMORROW, self.gate_a, 10).start,
                plate="В200ВВ77",
                make="ГАЗ",
                driver_name="Петров Петр Петрович",
                cargo_type="Паллеты",
                telegram_id=2,
                chat_id=2,
                now=NOW,
                vehicle="light",
                weight_kg=6000,
            )
        with self.assertRaises(BookingError) as missing:
            create_booking(
                ENGINE,
                gate_id=self.gate_b,
                slot_start=self._slot(TOMORROW, self.gate_b, 8).start,
                plate="А100АА77",
                make="ГАЗ",
                driver_name="Петров Петр Петрович",
                cargo_type="Паллеты",
                telegram_id=3,
                chat_id=3,
                now=NOW,
            )
        self.assertEqual(str(missing.exception), NOT_LISTED)

    def _book(
        self,
        day,
        hour: int,
        *,
        gate_id: int | None = None,
        telegram_id: int = 1,
        chat_id: int = 1,
        plate: str = "A100AA77",
        slot_start: datetime | None = None,
        driver_name: str = "Иванов Иван Иванович",
    ):
        gate = self.gate_a if gate_id is None else gate_id
        start = slot_start or self._slot(day, gate, hour).start
        return create_booking(
            ENGINE,
            gate_id=gate,
            slot_start=start,
            plate=plate,
            make="Volvo",
            driver_name=driver_name,
            cargo_type="паллеты",
            telegram_id=telegram_id,
            chat_id=chat_id,
            now=NOW,
        )

    def _slot(self, day, gate_id: int, hour: int):
        for slot in slots_for_gate(ENGINE, day, gate_id, now=NOW):
            local = slot.start.astimezone(ZONE)
            if local.hour == hour and local.minute == 0:
                return slot
        self.fail(f"no slot at {hour}:00")

    def _set_status(self, booking_id: int, status: BookingStatus) -> None:
        with Session(ENGINE) as session:
            with session.begin():
                row = session.get(Booking, booking_id)
                assert row is not None
                row.status = status
                if status is BookingStatus.late:
                    row.late_at = NOW
                if status is BookingStatus.arrived:
                    row.arrived_at = NOW

    def _status(self, booking_id: int) -> BookingStatus:
        with Session(ENGINE) as session:
            row = session.get(Booking, booking_id)
            assert row is not None
            return row.status

    def _start(self, booking_id: int) -> datetime:
        with Session(ENGINE) as session:
            row = session.get(Booking, booking_id)
            assert row is not None
            return row.slot_start

    def _occupying(self, gate_id: int, start: datetime) -> int:
        with Session(ENGINE) as session:
            count = session.scalar(
                select(func.count())
                .select_from(Booking)
                .where(
                    Booking.gate_id == gate_id,
                    Booking.slot_start == start,
                    Booking.status.in_(tuple(OCCUPYING_STATUSES)),
                )
            )
            return int(count or 0)

    def _count(self) -> int:
        with Session(ENGINE) as session:
            count = session.scalar(select(func.count()).select_from(Booking))
            return int(count or 0)

    def _wipe(self) -> None:
        with Session(ENGINE) as session:
            with session.begin():
                session.execute(delete(BookingStatusEvent))
                session.execute(delete(NotificationLog))
                session.execute(delete(Booking))
                session.execute(delete(ClosedSlot))
                session.execute(delete(GateHour))
                session.execute(delete(Gate))
                session.execute(delete(SiteSettings))


if __name__ == "__main__":
    unittest.main()
