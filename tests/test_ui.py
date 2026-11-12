"""Подписи экранов: клавиатура, госномер, строка брони, фильтр смены, текст напоминания."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dockbot.booking import FieldError, Trip, normalize_plate
from dockbot.bot.keyboard import button_labels, keyboard_for
from dockbot.models import BookingStatus, StaffRole
from dockbot.present import day_button, trip_button
from dockbot.reminders import late_text, reminder_text
from dockbot.models import NotificationKind
from dockbot.shiftboard import filter_shift

ZONE = ZoneInfo("Europe/Moscow")


def _trip(status: BookingStatus, hour: int) -> Trip:
    start = datetime(2026, 11, 12, hour, 0, tzinfo=ZONE)
    return Trip(
        id=hour,
        gate_id=1,
        gate_code="2",
        gate_name="Ворота 2",
        slot_start=start,
        slot_end=start.replace(hour=hour + 1),
        plate="А123АА77",
        make="КАМАЗ",
        driver_name="Иванов",
        cargo_type="Паллеты",
        status=status,
        pass_token="token",
        pass_pin="330869",
        driver_telegram_id=1,
        driver_chat_id=1,
    )


class ScreenTests(unittest.TestCase):
    def test_three_roles_stay_on_every_keyboard(self) -> None:
        for role in (None, StaffRole.security, StaffRole.warehouse):
            markup = keyboard_for(role)
            self.assertTrue(markup.resize_keyboard)
            self.assertTrue(markup.is_persistent)
            self.assertEqual(
                button_labels(markup)[-3:],
                ["Водитель", "Охрана", "Кладовщик"],
            )
        self.assertIn("Записаться", button_labels(keyboard_for(None)))
        self.assertIn("Смена сегодня", button_labels(keyboard_for(StaffRole.security)))
        self.assertNotIn(
            "Сканировать QR", button_labels(keyboard_for(StaffRole.warehouse))
        )

    def test_plate_accepts_latin_lookalikes_and_rejects_a_short_one(self) -> None:
        self.assertEqual(normalize_plate(" a100aa77 "), "А100АА77")
        self.assertEqual(normalize_plate("А123ВС178"), "А123ВС178")
        with self.assertRaises(FieldError) as caught:
            normalize_plate("A1")
        self.assertIn("А123ВС178", str(caught.exception))

    def test_date_and_trip_buttons_are_short_russian(self) -> None:
        self.assertEqual(day_button(date(2026, 11, 9)), "Пн 09.11")
        label = trip_button(_trip(BookingStatus.confirmed, 9), ZONE)
        self.assertEqual(label, "12.11 09:00 · В2 · А123АА77 · Подтверждено")

    def test_shift_filters_and_reminder_copy(self) -> None:
        trips = [
            _trip(BookingStatus.booked, 8),
            _trip(BookingStatus.arrived, 9),
            _trip(BookingStatus.completed, 10),
            _trip(BookingStatus.late, 11),
        ]
        moment = datetime(2026, 11, 12, 9, 30, tzinfo=ZONE)
        self.assertEqual([item.status for item in filter_shift(trips, ZONE, "wait", moment)], [BookingStatus.booked])
        self.assertEqual(
            [item.status for item in filter_shift(trips, ZONE, "onsite", moment)],
            [BookingStatus.arrived],
        )
        self.assertEqual(
            [item.status for item in filter_shift(trips, ZONE, "done", moment)],
            [BookingStatus.completed, BookingStatus.late],
        )
        self.assertEqual(
            [item.slot_start.hour for item in filter_shift(trips, ZONE, "hour", moment)],
            [9],
        )
        start = trips[0].slot_start
        end = trips[0].slot_end
        text = reminder_text(start, end, "1", "А123АА77", ZONE, NotificationKind.h24)
        self.assertIn("Напоминание: разгрузка", text)
        self.assertIn("ворота 1", text)
        self.assertNotIn("А123АА77", text)
        late = late_text(start, end, "1", "А123АА77", ZONE)
        self.assertIn("Запишитесь снова", late)


if __name__ == "__main__":
    unittest.main()
