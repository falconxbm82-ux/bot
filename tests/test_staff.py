"""Персонал: PIN, пропуск на КПП, статусы и экран смены."""

from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
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

from dockbot.access import (  # noqa: E402
    AccessDenied,
    active_staff,
    enter_pin,
    hash_pin,
    unusable_pin_hash,
    verify_pin,
)
from dockbot.booking import (  # noqa: E402
    SlotTaken,
    Trip,
    create_booking,
    slots_for_gate,
)
from dockbot.bot.driver import driver_menu  # noqa: E402
from dockbot.bot.staff import menu_rows  # noqa: E402
from dockbot.checkpoint import (  # noqa: E402
    PassNotFound,
    TransitionRejected,
    apply_mark,
    find_pass,
    read_pass_image,
)
from dockbot.config import ConfigError, parse_staff_list  # noqa: E402
from dockbot.configure import apply_staff  # noqa: E402
from dockbot.db import make_engine, upgrade_head  # noqa: E402
from dockbot.models import (  # noqa: E402
    Booking,
    BookingStatus,
    BookingStatusEvent,
    Gate,
    GateHour,
    NotificationLog,
    SiteSettings,
    StaffRole,
    StaffUser,
    StatusSource,
)
from dockbot.passes import new_token, qr_png  # noqa: E402
from dockbot.qrscan import decode_qr  # noqa: E402
from dockbot.shiftboard import (  # noqa: E402
    checkpoint_actions,
    load_shift,
    row_actions,
    shift_pages,
)

ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
TODAY = NOW.astimezone(ZONE).date()
TOMORROW = TODAY + timedelta(days=1)
ENGINE = make_engine(os.environ["DATABASE_URL"])
STAFF_TG = 501
OTHER_TG = 502
WAREHOUSE_TG = 503
DRIVER = 301


def setUpModule() -> None:
    upgrade_head()


def tearDownModule() -> None:
    ENGINE.dispose()


class StaffTests(unittest.TestCase):
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

    def test_roles_split_pass_check_and_shift_marks(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        self._warehouse()
        trip = self._book(TOMORROW, 8)
        with self.assertRaises(AccessDenied):
            find_pass(ENGINE, WAREHOUSE_TG, pin=trip.pass_pin)
        with self.assertRaises(AccessDenied):
            read_pass_image(ENGINE, WAREHOUSE_TG, b"not-a-picture")
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.arrived, now=NOW)
        self.assertEqual(self._status(trip.id), BookingStatus.booked)
        found = find_pass(ENGINE, STAFF_TG, pin=trip.pass_pin)
        self.assertEqual(found.id, trip.id)
        apply_mark(ENGINE, STAFF_TG, trip.id, BookingStatus.arrived, now=NOW)
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, STAFF_TG, trip.id, BookingStatus.unloading, now=NOW)
        self.assertEqual(self._status(trip.id), BookingStatus.arrived)
        marked = apply_mark(
            ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.unloading, now=NOW
        )
        self.assertEqual(marked.status, BookingStatus.unloading)
        self.assertEqual(row_actions(marked)[0].label, "Разгружен")
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, STAFF_TG, trip.id, BookingStatus.completed, now=NOW)
        done = apply_mark(
            ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.completed, now=NOW
        )
        self.assertEqual(done.status, BookingStatus.completed)

    def test_driver_menu_hides_staff_actions(self) -> None:
        plain = _labels(driver_menu())
        self.assertNotIn("Проверить пропуск", plain)
        self.assertNotIn("Экран смены", plain)
        guard = _labels(driver_menu(menu_rows(StaffRole.security)))
        self.assertIn("Забронировать", guard)
        self.assertIn("Проверить пропуск", guard)
        self.assertNotIn("Экран смены", guard)
        warehouse = _labels(driver_menu(menu_rows(StaffRole.warehouse)))
        self.assertIn("Экран смены", warehouse)
        self.assertNotIn("Проверить пропуск", warehouse)

    def test_staff_pin_binds_empty_id_and_does_not_overwrite(self) -> None:
        staff_id = self._staff(pin="246810")
        person = enter_pin(ENGINE, STAFF_TG, "246810")
        self.assertEqual(person.telegram_id, STAFF_TG)
        self.assertEqual(person.role, StaffRole.security)
        again = enter_pin(ENGINE, STAFF_TG, "246810")
        self.assertEqual(again.id, person.id)
        with self.assertRaises(AccessDenied):
            enter_pin(ENGINE, OTHER_TG, "246810")
        with Session(ENGINE) as session:
            row = session.get(StaffUser, staff_id)
            assert row is not None
            self.assertEqual(row.telegram_id, STAFF_TG)
            self.assertNotIn("246810", row.pin_hash)

    def test_pass_pin_does_not_grant_staff_and_wrong_pin_is_generic(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        trip = self._book(TOMORROW, 8)
        with self.assertRaises(AccessDenied):
            enter_pin(ENGINE, OTHER_TG, trip.pass_pin)
        with self.assertRaises(AccessDenied):
            enter_pin(ENGINE, OTHER_TG, "0000")
        self.assertIsNone(active_staff(ENGINE, OTHER_TG))
        self.assertEqual(self._status(trip.id), BookingStatus.booked)

    def test_known_telegram_id_works_and_disabled_staff_loses_access(self) -> None:
        self._staff(pin=None, telegram_id=STAFF_TG)
        person = active_staff(ENGINE, STAFF_TG)
        assert person is not None
        self.assertEqual(person.role, StaffRole.security)
        with self.assertRaises(AccessDenied):
            enter_pin(ENGINE, OTHER_TG, "246810")
        with Session(ENGINE) as session:
            with session.begin():
                row = session.scalar(
                    select(StaffUser).where(StaffUser.telegram_id == STAFF_TG)
                )
                assert row is not None
                row.is_active = False
        self.assertIsNone(active_staff(ENGINE, STAFF_TG))
        trip = self._book(TOMORROW, 8)
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, STAFF_TG, trip.id, BookingStatus.arrived, now=NOW)
        self.assertEqual(self._status(trip.id), BookingStatus.booked)

    def test_one_telegram_id_cannot_take_a_second_role(self) -> None:
        self._staff(name="Охрана", pin="246810", telegram_id=STAFF_TG)
        self._staff(
            name="Склад",
            pin="135790",
            role=StaffRole.warehouse,
            telegram_id=None,
        )
        with self.assertRaises(AccessDenied):
            enter_pin(ENGINE, STAFF_TG, "135790")
        with Session(ENGINE) as session:
            warehouse = session.scalar(
                select(StaffUser).where(StaffUser.display_name == "Склад")
            )
            assert warehouse is not None
            self.assertIsNone(warehouse.telegram_id)
        with self.assertRaises(IntegrityError):
            self._staff(
                name="Вторая охрана",
                pin="975310",
                telegram_id=STAFF_TG,
            )

    def test_config_hashes_pin_and_does_not_replace_a_bound_id(self) -> None:
        spec = parse_staff_list("security:Охрана:246810")[0]
        self.assertNotIn("246810", repr(spec))
        apply_staff(ENGINE, (spec,))
        with Session(ENGINE) as session:
            row = session.scalar(select(StaffUser))
            assert row is not None
            self.assertIsNone(row.telegram_id)
            self.assertTrue(verify_pin("246810", row.pin_hash))
            self.assertNotIn("246810", row.pin_hash)
        apply_staff(ENGINE, (parse_staff_list("security:Охрана:246810:501")[0],))
        apply_staff(ENGINE, (parse_staff_list("security:Охрана:246810:999")[0],))
        with Session(ENGINE) as session:
            rows = session.scalars(select(StaffUser)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].telegram_id, STAFF_TG)
            self.assertTrue(verify_pin("246810", rows[0].pin_hash))
        apply_staff(ENGINE, (parse_staff_list("warehouse:Склад:135790:501")[0],))
        with Session(ENGINE) as session:
            by_name = {
                row.display_name: row.telegram_id
                for row in session.scalars(select(StaffUser)).all()
            }
        self.assertEqual(by_name["Охрана"], STAFF_TG)
        self.assertIsNone(by_name["Склад"])

    def test_staff_list_parser_rejects_a_bad_record(self) -> None:
        self.assertEqual(parse_staff_list(None), ())
        self.assertEqual(parse_staff_list("  "), ())
        only_id = parse_staff_list("warehouse:Склад::42")[0]
        self.assertIsNone(only_id.pin)
        self.assertEqual(only_id.telegram_id, 42)
        for raw in (
            "boss:Имя:246810",
            "security:Имя:12",
            "security:Имя:12345",
            "security:Имя:1234567",
            "security:Имя:abcdef",
            "security:Имя::",
            "security:А:246810:5;warehouse:Б:135790:5",
        ):
            with self.assertRaises(ConfigError):
                parse_staff_list(raw)

    def test_unreadable_photo_and_wrong_pin_do_not_change_status(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        trip = self._book(TOMORROW, 8)
        blank = _blank_png()
        self.assertIsNone(decode_qr(b"not-a-picture"))
        self.assertIsNone(decode_qr(blank))
        before = self._event_count(trip.id)
        self.assertIsNone(read_pass_image(ENGINE, STAFF_TG, blank))
        with self.assertRaises(PassNotFound):
            find_pass(ENGINE, STAFF_TG, pin="000000")
        with self.assertRaises(PassNotFound):
            read_pass_image(ENGINE, STAFF_TG, qr_png("missing-token"))
        self.assertEqual(self._status(trip.id), BookingStatus.booked)
        self.assertIsNone(self._column(trip.id, "arrived_at"))
        self.assertEqual(self._event_count(trip.id), before)
        found = read_pass_image(ENGINE, STAFF_TG, qr_png(trip.pass_token))
        assert found is not None
        self.assertEqual(found.id, trip.id)
        self.assertEqual(self._status(trip.id), BookingStatus.booked)
        with self.assertRaises(AccessDenied):
            read_pass_image(ENGINE, OTHER_TG, qr_png(trip.pass_token))
        with self.assertRaises(AccessDenied):
            find_pass(ENGINE, OTHER_TG, pin=trip.pass_pin)

    def test_active_pin_wins_and_archive_does_not_mark_arrived(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        active = self._insert(
            start=_at(TODAY, 10),
            status=BookingStatus.booked,
            pin="222222",
            plate="ACTIVE1",
            created_at=NOW,
        )
        self._insert(
            start=_at(TODAY, 8),
            status=BookingStatus.cancelled,
            pin="222222",
            plate="OLD1",
            created_at=NOW - timedelta(days=1),
        )
        found = find_pass(ENGINE, STAFF_TG, pin="222222")
        self.assertEqual(found.id, active)
        older = self._insert(
            start=_at(TODAY, 8, gate="2"),
            status=BookingStatus.late,
            pin="333333",
            plate="OLDER",
            created_at=NOW - timedelta(hours=2),
            gate_id=self.gate_b,
        )
        newer = self._insert(
            start=_at(TODAY, 11),
            status=BookingStatus.completed,
            pin="333333",
            plate="NEWER",
            created_at=NOW,
        )
        archived = find_pass(ENGINE, STAFF_TG, pin="333333")
        self.assertEqual(archived.id, newer)
        self.assertNotEqual(archived.id, older)
        self.assertEqual(checkpoint_actions(archived), ())
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, STAFF_TG, archived.id, BookingStatus.arrived, now=NOW)
        self.assertEqual(self._status(archived.id), BookingStatus.completed)
        self.assertIsNone(self._column(archived.id, "arrived_at"))

    def test_arrived_only_from_booked_or_confirmed_and_chain_is_forward(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        self._warehouse()
        booked = self._book(TOMORROW, 8)
        self.assertEqual([item.label for item in checkpoint_actions(booked)], ["Прибыл"])
        self.assertEqual(row_actions(booked), ())
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, STAFF_TG, booked.id, BookingStatus.unloading, now=NOW)
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, WAREHOUSE_TG, booked.id, BookingStatus.unloading, now=NOW)
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, WAREHOUSE_TG, booked.id, BookingStatus.completed, now=NOW)
        self.assertEqual(self._status(booked.id), BookingStatus.booked)

        arrived = apply_mark(
            ENGINE, STAFF_TG, booked.id, BookingStatus.arrived, now=NOW
        )
        self.assertEqual(arrived.status, BookingStatus.arrived)
        self.assertEqual(checkpoint_actions(arrived), ())
        self.assertEqual(row_actions(arrived)[0].label, "На воротах №1")
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, STAFF_TG, booked.id, BookingStatus.arrived, now=NOW)
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, STAFF_TG, booked.id, BookingStatus.completed, now=NOW)
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, WAREHOUSE_TG, booked.id, BookingStatus.completed, now=NOW)
        self.assertEqual(self._status(booked.id), BookingStatus.arrived)

        confirmed = self._book(TODAY, 10)
        self.assertEqual(confirmed.status, BookingStatus.confirmed)
        apply_mark(ENGINE, STAFF_TG, confirmed.id, BookingStatus.arrived, now=NOW)
        with self.assertRaises(SlotTaken):
            self._create(confirmed.slot_start, telegram_id=DRIVER + 1, plate="B200BB77")
        self.assertEqual(self._status(confirmed.id), BookingStatus.arrived)

    def test_gate_and_unloaded_follow_the_chain_and_free_the_slot(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        self._warehouse()
        trip = self._book(TODAY, 10)
        self.assertEqual(trip.status, BookingStatus.confirmed)
        apply_mark(ENGINE, STAFF_TG, trip.id, BookingStatus.arrived, now=NOW)
        apply_mark(ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.unloading, now=NOW)
        done = apply_mark(
            ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.completed, now=NOW
        )
        self.assertEqual(done.status, BookingStatus.completed)
        self.assertEqual(row_actions(done), ())
        self.assertEqual(checkpoint_actions(done), ())
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.unloading, now=NOW)
        with self.assertRaises(AccessDenied):
            apply_mark(ENGINE, WAREHOUSE_TG, trip.id, BookingStatus.arrived, now=NOW)
        self.assertEqual(self._status(trip.id), BookingStatus.completed)
        again = self._create(
            trip.slot_start, telegram_id=DRIVER + 1, plate="B200BB77"
        )
        self.assertNotEqual(again.id, trip.id)
        with Session(ENGINE) as session:
            booking = session.get(Booking, trip.id)
            assert booking is not None
            self.assertIsNotNone(booking.arrived_at)
            self.assertIsNotNone(booking.unloading_at)
            self.assertIsNotNone(booking.completed_at)
            events = session.scalars(
                select(BookingStatusEvent)
                .where(
                    BookingStatusEvent.booking_id == trip.id,
                    BookingStatusEvent.source == StatusSource.staff,
                )
                .order_by(BookingStatusEvent.id)
            ).all()
            self.assertEqual(
                [event.to_status for event in events],
                [
                    BookingStatus.arrived,
                    BookingStatus.unloading,
                    BookingStatus.completed,
                ],
            )
            self.assertEqual(
                [event.from_status for event in events],
                [
                    BookingStatus.confirmed,
                    BookingStatus.arrived,
                    BookingStatus.unloading,
                ],
            )
            self.assertEqual(
                [event.actor_telegram_id for event in events],
                [STAFF_TG, WAREHOUSE_TG, WAREHOUSE_TG],
            )
            notices = session.scalar(
                select(func.count()).select_from(NotificationLog)
            )
        self.assertEqual(notices, 0)

    def test_late_and_cancelled_cannot_move_backward(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        late = self._book(TOMORROW, 9)
        self._set_status(late.id, BookingStatus.late)
        found = find_pass(ENGINE, STAFF_TG, pin=late.pass_pin)
        self.assertEqual(found.status, BookingStatus.late)
        self.assertEqual(checkpoint_actions(found), ())
        self.assertEqual(row_actions(found), ())
        with self.assertRaises(TransitionRejected):
            apply_mark(ENGINE, STAFF_TG, late.id, BookingStatus.arrived, now=NOW)
        for target in (BookingStatus.unloading, BookingStatus.completed):
            with self.assertRaises(AccessDenied):
                apply_mark(ENGINE, STAFF_TG, late.id, target, now=NOW)
        self.assertEqual(self._status(late.id), BookingStatus.late)
        cancelled = self._book(TOMORROW, 10)
        self._set_status(cancelled.id, BookingStatus.cancelled)
        with self.assertRaises(TransitionRejected):
            apply_mark(
                ENGINE, STAFF_TG, cancelled.id, BookingStatus.arrived, now=NOW
            )
        self.assertEqual(self._status(cancelled.id), BookingStatus.cancelled)

    def test_concurrent_arrival_keeps_one_status(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        trip = self._book(TOMORROW, 11)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def worker() -> None:
            barrier.wait()
            try:
                apply_mark(
                    ENGINE, STAFF_TG, trip.id, BookingStatus.arrived, now=NOW
                )
                results.append("ok")
            except TransitionRejected:
                results.append("no")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["no", "ok"])
        self.assertEqual(self._status(trip.id), BookingStatus.arrived)
        self.assertEqual(self._staff_event_count(trip.id), 1)

    def test_shift_board_is_today_grouped_by_hour(self) -> None:
        self._staff(pin="246810", telegram_id=STAFF_TG)
        self._warehouse()
        self._insert(
            start=_at(TODAY, 8),
            status=BookingStatus.late,
            pin="444444",
            plate="LATE1",
            name="Опоздавший",
        )
        self._insert(
            start=_at(TODAY, 8, gate="2"),
            status=BookingStatus.completed,
            pin="555555",
            plate="DONE1",
            name="Готов",
            gate_id=self.gate_b,
        )
        self._insert(
            start=_at(TODAY, 10),
            status=BookingStatus.booked,
            pin="666666",
            plate="BOOK1",
            name="Иванов",
        )
        self._insert(
            start=_at(TODAY, 11),
            status=BookingStatus.cancelled,
            pin="777777",
            plate="STOP1",
            name="Снят",
        )
        self._insert(
            start=_at(TOMORROW, 10),
            status=BookingStatus.booked,
            pin="888888",
            plate="NEXT1",
            name="Завтра",
        )
        self._insert(
            start=_at(TODAY - timedelta(days=1), 10),
            status=BookingStatus.completed,
            pin="999999",
            plate="YEST1",
            name="Вчера",
        )
        _zone, day, trips = load_shift(ENGINE, WAREHOUSE_TG, now=NOW)
        self.assertEqual(day, TODAY)
        plates = [trip.plate for trip in trips]
        self.assertEqual(plates, ["LATE1", "DONE1", "BOOK1", "STOP1"])
        hours = [trip.slot_start.astimezone(ZONE).hour for trip in trips]
        self.assertEqual(hours, [8, 8, 10, 11])
        pages = shift_pages(trips, ZONE, day)
        text = "\n".join(page.text for page in pages)
        self.assertIn("Смена 12.11.2026", text)
        self.assertLess(text.index("\n08:00\n"), text.index("\n10:00\n"))
        self.assertIn("LATE1", text)
        self.assertIn("Опоздал", text)
        self.assertIn("DONE1", text)
        self.assertIn("Завершено", text)
        self.assertIn("BOOK1", text)
        self.assertIn("STOP1", text)
        self.assertNotIn("NEXT1", text)
        self.assertNotIn("YEST1", text)
        self.assertEqual(pages[0].text.splitlines()[1], "08:00")
        with self.assertRaises(AccessDenied):
            load_shift(ENGINE, OTHER_TG, now=NOW)
        with self.assertRaises(AccessDenied):
            load_shift(ENGINE, STAFF_TG, now=NOW)

    def test_empty_shift_says_there_are_no_records(self) -> None:
        self._warehouse()
        _zone, day, trips = load_shift(ENGINE, WAREHOUSE_TG, now=NOW)
        pages = shift_pages(trips, ZONE, day)
        self.assertEqual(len(pages), 1)
        self.assertIn("Записей нет.", pages[0].text)
        self.assertEqual(pages[0].trips, ())

    def test_long_shift_keeps_hour_order_across_messages(self) -> None:
        trips = [
            _fake(index, hour, f"P{index}")
            for index, hour in enumerate((8, 8, 9, 10, 10, 11), start=1)
        ]
        pages = shift_pages(trips, ZONE, TODAY, limit=80)
        self.assertGreater(len(pages), 1)
        ids = [trip.id for page in pages for trip in page.trips]
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6])
        headers: list[str] = []
        for page in pages:
            for line in page.text.splitlines():
                if len(line) == 5 and line[2] == ":":
                    headers.append(line)
        self.assertEqual(headers, sorted(headers))

    def _book(
        self,
        day,
        hour: int,
        *,
        telegram_id: int = DRIVER,
        gate_id: int | None = None,
        plate: str = "A100AA77",
    ) -> Trip:
        gate_id = self.gate_a if gate_id is None else gate_id
        for slot in slots_for_gate(ENGINE, day, gate_id, now=NOW):
            if slot.start.astimezone(ZONE).hour == hour:
                return create_booking(
                    ENGINE,
                    gate_id=gate_id,
                    slot_start=slot.start,
                    plate=plate,
                    make="КАМАЗ",
                    driver_name="Иванов",
                    cargo_type="Паллеты",
                    telegram_id=telegram_id,
                    chat_id=telegram_id,
                    now=NOW,
                )
        raise AssertionError(hour)

    def _create(
        self, slot_start: datetime, *, telegram_id: int, plate: str
    ) -> Trip:
        return create_booking(
            ENGINE,
            gate_id=self.gate_a,
            slot_start=slot_start,
            plate=plate,
            make="КАМАЗ",
            driver_name="Петров",
            cargo_type="Коробки",
            telegram_id=telegram_id,
            chat_id=telegram_id,
            now=NOW,
        )

    def _warehouse(self) -> int:
        return self._staff(
            pin="135790",
            telegram_id=WAREHOUSE_TG,
            name="Склад",
            role=StaffRole.warehouse,
        )

    def _staff(
        self,
        *,
        pin: str | None,
        telegram_id: int | None = None,
        name: str = "Охрана",
        role: StaffRole = StaffRole.security,
    ) -> int:
        with Session(ENGINE) as session:
            with session.begin():
                row = StaffUser(
                    telegram_id=telegram_id,
                    role=role,
                    display_name=name,
                    pin_hash=hash_pin(pin) if pin else unusable_pin_hash(),
                    is_active=True,
                )
                session.add(row)
                session.flush()
                return row.id

    def _insert(
        self,
        *,
        start: datetime,
        status: BookingStatus,
        pin: str,
        plate: str,
        name: str = "Иванов",
        created_at: datetime | None = None,
        gate_id: int | None = None,
    ) -> int:
        with Session(ENGINE) as session:
            with session.begin():
                booking = Booking(
                    gate_id=self.gate_a if gate_id is None else gate_id,
                    slot_start=start,
                    slot_end=start + timedelta(hours=1),
                    plate=plate,
                    make="КАМАЗ",
                    driver_name=name,
                    cargo_type="Паллеты",
                    driver_telegram_id=DRIVER,
                    driver_chat_id=DRIVER,
                    status=status,
                    pass_token=new_token(),
                    pass_pin=pin,
                    created_at=created_at or start,
                )
                session.add(booking)
                session.flush()
                return booking.id

    def _set_status(self, booking_id: int, status: BookingStatus) -> None:
        with Session(ENGINE) as session:
            with session.begin():
                booking = session.get(Booking, booking_id)
                assert booking is not None
                booking.status = status

    def _status(self, booking_id: int) -> BookingStatus:
        with Session(ENGINE) as session:
            booking = session.get(Booking, booking_id)
            assert booking is not None
            return booking.status

    def _column(self, booking_id: int, name: str):
        with Session(ENGINE) as session:
            booking = session.get(Booking, booking_id)
            assert booking is not None
            return getattr(booking, name)

    def _event_count(self, booking_id: int) -> int:
        with Session(ENGINE) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(BookingStatusEvent)
                    .where(BookingStatusEvent.booking_id == booking_id)
                )
                or 0
            )

    def _staff_event_count(self, booking_id: int) -> int:
        with Session(ENGINE) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(BookingStatusEvent)
                    .where(
                        BookingStatusEvent.booking_id == booking_id,
                        BookingStatusEvent.source == StatusSource.staff,
                    )
                )
                or 0
            )

    def _wipe(self) -> None:
        with Session(ENGINE) as session:
            with session.begin():
                session.execute(delete(BookingStatusEvent))
                session.execute(delete(NotificationLog))
                session.execute(delete(Booking))
                session.execute(delete(GateHour))
                session.execute(delete(Gate))
                session.execute(delete(SiteSettings))
                session.execute(delete(StaffUser))


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _at(day, hour: int, gate: str = "1") -> datetime:
    del gate
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZONE)


def _blank_png() -> bytes:
    _ok, encoded = cv2.imencode(".png", np.zeros((80, 80, 3), dtype=np.uint8))
    return encoded.tobytes()


def _fake(booking_id: int, hour: int, plate: str) -> Trip:
    start = datetime(2026, 11, 12, hour, 0, tzinfo=ZONE)
    return Trip(
        id=booking_id,
        gate_id=1,
        gate_code="1",
        gate_name="Ворота 1",
        slot_start=start,
        slot_end=start + timedelta(hours=1),
        plate=plate,
        make="КАМАЗ",
        driver_name="Иванов Иван Иванович",
        cargo_type="Паллеты",
        status=BookingStatus.booked,
        pass_token=f"token-{booking_id}",
        pass_pin=f"{booking_id:06d}",
        driver_telegram_id=DRIVER,
        driver_chat_id=DRIVER,
    )


if __name__ == "__main__":
    unittest.main()
