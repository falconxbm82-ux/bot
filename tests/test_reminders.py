"""Напоминания, подтверждение в T−2 ч и автоматическое опоздание."""

from __future__ import annotations

import logging
import os
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
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

from dockbot.booking import reschedule_booking  # noqa: E402
from dockbot.config import ConfigError, Settings  # noqa: E402
from dockbot.db import make_engine, upgrade_head  # noqa: E402
from dockbot.models import (  # noqa: E402
    Booking,
    BookingStatus,
    BookingStatusEvent,
    Gate,
    GateHour,
    NotificationKind,
    NotificationLog,
    SiteSettings,
    StatusSource,
)
from dockbot.passes import new_pin, new_token  # noqa: E402
from dockbot.reminders import (  # noqa: E402
    DeliveryBlocked,
    DeliveryFailed,
    catch_up,
    deliver,
    due_time,
)
from dockbot.scheduler import JOB_TABLE, make_scheduler, sync_one  # noqa: E402

ZONE = ZoneInfo("Europe/Moscow")
LATITUDE = 59.93
LONGITUDE = 30.31
ENGINE = make_engine(os.environ["DATABASE_URL"])
DRIVER = 301


def setUpModule() -> None:
    upgrade_head()


def tearDownModule() -> None:
    ENGINE.dispose()


class Recording:
    def __init__(self) -> None:
        self.reminders: list[tuple[int, float, float, str]] = []
        self.lates: list[tuple[int, str]] = []
        self.blocked: set[int] = set()
        self.fail = False

    def send_reminder(
        self, chat_id: int, latitude: float, longitude: float, text: str
    ) -> None:
        if chat_id in self.blocked:
            raise DeliveryBlocked()
        if self.fail:
            raise DeliveryFailed("TelegramRetryAfter")
        self.reminders.append((chat_id, latitude, longitude, text))

    def send_late(self, chat_id: int, text: str) -> None:
        if chat_id in self.blocked:
            raise DeliveryBlocked()
        self.lates.append((chat_id, text))


class ReminderTests(unittest.TestCase):
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
                gate = Gate(code="1", name="Ворота 1")
                session.add(gate)
                session.flush()
                self.gate_id = gate.id
                for weekday in range(1, 8):
                    session.add(
                        GateHour(
                            gate_id=gate.id,
                            weekday=weekday,
                            start_time=time(8, 0),
                            end_time=time(12, 0),
                            slot_minutes=60,
                        )
                    )
        self.sender = Recording()

    def tearDown(self) -> None:
        self._wipe()

    def test_empty_coordinates_refuse_the_reminder_service(self) -> None:
        settings = Settings(
            bot_token="test-token",
            database_url="postgresql+psycopg://localhost/dockbot",
            time_zone="Europe/Moscow",
            latitude=None,
            longitude=None,
            cancel_window_hours=2,
            gates=(),
            weekdays=(),
            gate_open=None,
            gate_close=None,
            slot_minutes=None,
            staff=(),
        )
        self.assertFalse(settings.site_is_complete)
        with self.assertRaises(ConfigError):
            settings.require_site()

    def test_points_already_past_at_creation_are_not_sent(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(hours=3)
        booking_id = self._insert(slot, created_at=created)
        now = due_time(slot, NotificationKind.h2)
        self._deliver(booking_id, NotificationKind.h24, now)
        self._deliver(booking_id, NotificationKind.h2, now)
        self._deliver(booking_id, NotificationKind.m30, now)
        self.assertEqual(len(self.sender.reminders), 1)
        _chat, latitude, longitude, text = self.sender.reminders[0]
        self.assertEqual(latitude, LATITUDE)
        self.assertEqual(longitude, LONGITUDE)
        self.assertIn("20.11.2026", text)
        self.assertIn("08:00", text)
        self.assertIn("Через 2 часа", text)
        self.assertIn("ворота 1", text)
        self.assertEqual(self._status(booking_id), BookingStatus.confirmed)
        self.assertIsNotNone(self._column(booking_id, "reminded_2_at"))
        self.assertIsNone(self._column(booking_id, "reminded_24_at"))
        with Session(ENGINE) as session:
            event = session.scalar(
                select(BookingStatusEvent).where(
                    BookingStatusEvent.booking_id == booking_id,
                    BookingStatusEvent.source == StatusSource.scheduler,
                )
            )
            assert event is not None
            self.assertEqual(event.from_status, BookingStatus.booked)
            self.assertEqual(event.to_status, BookingStatus.confirmed)
            self.assertIsNone(event.actor_telegram_id)

    def test_catch_up_sends_a_reminder_that_came_due_while_down(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(days=3)
        booking_id = self._insert(slot, created_at=created)
        now = slot - timedelta(minutes=10)
        catch_up(
            ENGINE,
            sender=self.sender,
            now=now,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )
        self.assertEqual(len(self.sender.reminders), 3)
        self.assertEqual(self.sender.lates, [])
        self.assertEqual(self._status(booking_id), BookingStatus.confirmed)
        again = Recording()
        catch_up(
            ENGINE,
            sender=again,
            now=now,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )
        self.assertEqual(again.reminders, [])

    def test_sent_mark_is_written_only_after_accept(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(days=3)
        booking_id = self._insert(slot, created_at=created)
        now = due_time(slot, NotificationKind.h24)
        self.sender.fail = True
        self._deliver(booking_id, NotificationKind.h24, now)
        self.assertIsNone(self._column(booking_id, "reminded_24_at"))
        with Session(ENGINE) as session:
            row = session.scalar(
                select(NotificationLog).where(
                    NotificationLog.booking_id == booking_id,
                    NotificationLog.kind == NotificationKind.h24,
                )
            )
            assert row is not None
            self.assertFalse(row.success)
            self.assertEqual(row.error_text, "TelegramRetryAfter")
        self.sender.fail = False
        self._deliver(booking_id, NotificationKind.h24, now)
        self.assertIsNone(self._column(booking_id, "reminded_24_at"))
        self.assertEqual(self.sender.reminders, [])

    def test_blocked_chat_is_logged_and_the_next_trip_is_sent(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(days=3)
        blocked_id = self._insert(slot, created_at=created, chat_id=401, plate="BLOCK1")
        sent_id = self._insert(
            _moment(2026, 11, 20, 9),
            created_at=created,
            chat_id=402,
            plate="SEND1",
        )
        self.sender.blocked.add(401)
        catch_up(
            ENGINE,
            sender=self.sender,
            now=due_time(_moment(2026, 11, 20, 9), NotificationKind.h24),
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )
        self.assertEqual([item[0] for item in self.sender.reminders], [402])
        self.assertIsNone(self._column(blocked_id, "reminded_24_at"))
        self.assertIsNotNone(self._column(sent_id, "reminded_24_at"))
        self.assertIn("Напоминание: разгрузка", self.sender.reminders[0][3])

    def test_arrival_before_the_threshold_blocks_lateness(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        booking_id = self._insert(slot, created_at=slot - timedelta(days=2))
        self._set_status(booking_id, BookingStatus.arrived)
        self._deliver(
            booking_id, NotificationKind.late, slot + timedelta(minutes=14)
        )
        self._deliver(
            booking_id, NotificationKind.late, slot + timedelta(minutes=15)
        )
        self.assertEqual(self._status(booking_id), BookingStatus.arrived)
        self.assertEqual(self.sender.lates, [])

    def test_lateness_at_fifteen_minutes_frees_the_slot_and_sends_once(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(days=2)
        booking_id = self._insert(slot, created_at=created, plate="LATE1")
        now = slot + timedelta(minutes=15)
        # Alembic fileConfig отключает уже созданные журналы. Для проверки строки его включают обратно.
        logging.getLogger("dockbot.reminders").disabled = False
        with self.assertLogs("dockbot.reminders", level="INFO") as caught:
            catch_up(
                ENGINE,
                sender=self.sender,
                now=now,
                latitude=LATITUDE,
                longitude=LONGITUDE,
                zone=ZONE,
            )
        self.assertTrue(
            any(f"lateness recorded booking={booking_id}" in line for line in caught.output)
        )
        self.assertEqual(self.sender.reminders, [])
        self.assertEqual(len(self.sender.lates), 1)
        self.assertIn("снято", self.sender.lates[0][1])
        self.assertIn("Запишитесь снова", self.sender.lates[0][1])
        self.assertIn("08:00", self.sender.lates[0][1])
        self.assertEqual(self._status(booking_id), BookingStatus.late)
        self.assertIsNotNone(self._column(booking_id, "late_at"))
        self._insert(
            slot,
            created_at=now,
            status=BookingStatus.booked,
            plate="NEXT1",
            chat_id=999,
        )
        catch_up(
            ENGINE,
            sender=Recording(),
            now=now,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )
        self.assertEqual(len(self.sender.lates), 1)

    def test_restarted_delivery_does_not_send_the_reminder_again(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        booking_id = self._insert(slot, created_at=slot - timedelta(days=2))
        now = due_time(slot, NotificationKind.h24)
        self._deliver(booking_id, NotificationKind.h24, now)
        self.assertEqual(len(self.sender.reminders), 1)
        restarted = Recording()
        deliver(
            ENGINE,
            booking_id,
            NotificationKind.h24,
            sender=restarted,
            now=now,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )
        self.assertEqual(restarted.reminders, [])
        self.assertEqual(len(self.sender.reminders), 1)

    def test_second_delivery_does_not_repeat_a_sent_reminder(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        booking_id = self._insert(slot, created_at=slot - timedelta(days=2))
        now = due_time(slot, NotificationKind.m30)
        self._deliver(booking_id, NotificationKind.m30, now)
        self._deliver(booking_id, NotificationKind.m30, now)
        self.assertEqual(len(self.sender.reminders), 1)

    def test_reschedule_clears_sent_marks_for_the_new_slot(self) -> None:
        from dockbot.booking import create_booking

        now = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
        tomorrow = now.astimezone(ZONE).date() + timedelta(days=1)
        first = _slot(tomorrow, 8, self.gate_id, now)
        second = _slot(tomorrow, 10, self.gate_id, now)
        trip = create_booking(
            ENGINE,
            gate_id=self.gate_id,
            slot_start=first,
            plate="A100AA77",
            make="КАМАЗ",
            driver_name="Иванов",
            cargo_type="Паллеты",
            telegram_id=DRIVER,
            chat_id=DRIVER,
            now=now,
        )
        with Session(ENGINE) as session:
            with session.begin():
                booking = session.get(Booking, trip.id)
                assert booking is not None
                booking.reminded_24_at = now
        moved = reschedule_booking(
            ENGINE,
            telegram_id=DRIVER,
            chat_id=DRIVER,
            booking_id=trip.id,
            gate_id=self.gate_id,
            slot_start=second,
            now=now,
        )
        self.assertIsNone(self._column(moved.id, "reminded_24_at"))
        self.assertIsNotNone(self._column(moved.id, "rescheduled_at"))

    def test_concurrent_deliveries_send_once(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        booking_id = self._insert(slot, created_at=slot - timedelta(days=2))
        now = due_time(slot, NotificationKind.h24)
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            deliver(
                ENGINE,
                booking_id,
                NotificationKind.h24,
                sender=self.sender,
                now=now,
                latitude=LATITUDE,
                longitude=LONGITUDE,
                zone=ZONE,
            )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.sender.reminders), 1)

    def test_jobs_survive_a_scheduler_restart(self) -> None:
        slot = _moment(2026, 11, 20, 8)
        created = slot - timedelta(hours=1)
        booking_id = self._insert(slot, created_at=created)
        first = make_scheduler(ENGINE)
        second = None
        first.start(paused=True)
        try:
            sync_one(first, ENGINE, booking_id, created)
            missed = f"dockbot-{booking_id}-h24"
            kept = f"dockbot-{booking_id}-m30"
            self.assertIsNone(first.get_job(missed))
            self.assertIsNotNone(first.get_job(kept))
            first.shutdown(wait=False)
            second = make_scheduler(ENGINE)
            second.start(paused=True)
            self.assertIsNone(second.get_job(missed))
            restored = second.get_job(kept)
            assert restored is not None
            self.assertEqual(restored.args, (booking_id, "m30"))
        finally:
            if first.running:
                first.shutdown(wait=False)
            if second is not None and second.running:
                second.shutdown(wait=False)

    def _deliver(self, booking_id: int, kind: NotificationKind, now: datetime) -> None:
        deliver(
            ENGINE,
            booking_id,
            kind,
            sender=self.sender,
            now=now,
            latitude=LATITUDE,
            longitude=LONGITUDE,
            zone=ZONE,
        )

    def _insert(
        self,
        slot: datetime,
        *,
        created_at: datetime,
        status: BookingStatus = BookingStatus.booked,
        plate: str = "A100AA77",
        chat_id: int = DRIVER,
    ) -> int:
        with Session(ENGINE) as session:
            with session.begin():
                booking = Booking(
                    gate_id=self.gate_id,
                    slot_start=slot,
                    slot_end=slot + timedelta(hours=1),
                    plate=plate,
                    make="КАМАЗ",
                    driver_name="Иванов",
                    cargo_type="Паллеты",
                    driver_telegram_id=DRIVER,
                    driver_chat_id=chat_id,
                    status=status,
                    pass_token=new_token(),
                    pass_pin=new_pin(),
                    created_at=created_at,
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

    def _wipe(self) -> None:
        with Session(ENGINE) as session:
            with session.begin():
                session.execute(delete(BookingStatusEvent))
                session.execute(delete(NotificationLog))
                session.execute(delete(Booking))
                session.execute(delete(GateHour))
                session.execute(delete(Gate))
                session.execute(delete(SiteSettings))
        with ENGINE.connect() as connection:
            try:
                connection.execute(text(f"DELETE FROM {JOB_TABLE}"))
                connection.commit()
            except Exception:
                connection.rollback()


def _moment(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=ZONE)


def _slot(day, hour: int, gate_id: int, now: datetime) -> datetime:
    from dockbot.booking import slots_for_gate

    for slot in slots_for_gate(ENGINE, day, gate_id, now=now):
        if slot.start.astimezone(ZONE).hour == hour:
            return slot.start
    raise AssertionError(hour)


if __name__ == "__main__":
    unittest.main()
