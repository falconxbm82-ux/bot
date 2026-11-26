"""Подписи экранов: клавиатура, госномер, строка брони, фильтр смены, текст напоминания."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dockbot.booking import (
    FieldError,
    GateChoice,
    SlotChoice,
    Trip,
    normalize_plate,
    slot_has_passed,
)
from dockbot.bot.keyboard import button_labels, help_text, keyboard_for
from dockbot.bot.ui import (
    back_menu_markup,
    cancel_step_markup,
    card_markup,
    cargo_markup,
    dates_markup,
    gates_markup,
    pass_done_markup,
    slots_markup,
    summary_markup,
    trips_markup,
    vehicle_markup,
    weight_markup,
)
from dockbot.models import BookingStatus, NotificationKind, StaffRole
from dockbot.present import day_button, trip_button
from dockbot.reminders import late_text, reminder_text
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
    def test_keyboards_have_no_role_row(self) -> None:
        driver = button_labels(keyboard_for(None))
        self.assertEqual(driver[-1], "Помощь")
        self.assertIn("Мой пропуск", driver)
        self.assertNotIn("Как проехать", driver)
        self.assertNotIn("Охрана", driver)
        done = [
            button.text
            for row in pass_done_markup(1).inline_keyboard
            for button in row
        ]
        self.assertNotIn("Геометка склада", done)
        self.assertIn("Показать пропуск", done)
        self.assertIn("Смена сегодня", button_labels(keyboard_for(StaffRole.security)))
        warehouse = button_labels(keyboard_for(StaffRole.warehouse))
        self.assertNotIn("Сканировать QR", warehouse)
        self.assertIn("Ворота", button_labels(keyboard_for(StaffRole.admin)))

    def test_plate_accepts_latin_lookalikes_and_rejects_a_short_one(self) -> None:
        self.assertEqual(normalize_plate(" a100aa77 "), "А100АА77")
        self.assertEqual(normalize_plate("А123ВС178"), "А123ВС178")
        with self.assertRaises(FieldError) as caught:
            normalize_plate("A1")
        self.assertIn("А123ВС178", str(caught.exception))

    def test_date_and_trip_buttons_are_short_russian(self) -> None:
        self.assertEqual(day_button(date(2026, 11, 9)), "Пн 09.11")
        label = trip_button(_trip(BookingStatus.confirmed, 9), ZONE)
        self.assertEqual(label, "12.11 09:00 В2 А123АА77 Подтверждено")
        banned = ("«", "»", "◀", "▶", "·", "🟡", "🟢", "pallets")
        trip = _trip(BookingStatus.confirmed, 9)
        start = trip.slot_start
        slot = SlotChoice(1, "2", "Ворота 2", start, start + timedelta(hours=1))
        markups = [
            back_menu_markup(),
            cancel_step_markup(),
            vehicle_markup(),
            weight_markup(),
            weight_markup("light"),
            cargo_markup({"pallets": 60}),
            cargo_markup({"boxes": 20}, "light"),
            dates_markup([date(2026, 11, 9)]),
            gates_markup([GateChoice(1, "2", "Ворота 2")]),
            slots_markup([slot], ZONE, start),
            summary_markup(False),
            trips_markup([trip], ZONE, 0),
            card_markup(trip, True),
        ]
        for markup in markups:
            for row in markup.inline_keyboard:
                for button in row:
                    for mark in banned:
                        self.assertNotIn(mark, button.text, button.text)
        labels = [
            button.text
            for markup in markups
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn("В меню", labels)
        self.assertIn("Изменить данные", labels)
        self.assertIn("Паллеты 60 мин", labels)
        self.assertIn("Коробки 20 мин", labels)
        self.assertIn("1 т", labels)
        self.assertIn("3 т", labels)
        self.assertNotIn("Срочный", labels)
        self.assertNotIn("30 т", [
            button.text
            for row in weight_markup("light").inline_keyboard
            for button in row
        ])
        self.assertIn("Назад", labels)

    def test_help_lists_only_real_commands(self) -> None:
        shared = ("/start — открыть меню", "/menu — вернуться в меню", "/help — показать справку", "/cancel — сбросить диалог")
        driver = help_text(None)
        for line in shared:
            self.assertIn(line, driver)
        self.assertIn("/staff — войти по PIN сотрудника", driver)
        self.assertIn("/logout — выйти из роли", driver)
        self.assertNotIn("/logout — выйти в меню водителя", driver)
        for role in (StaffRole.security, StaffRole.warehouse, StaffRole.admin):
            text = help_text(role)
            for line in shared:
                self.assertIn(line, text)
            self.assertIn("/staff — открыть меню сотрудника", text)
            self.assertIn("/logout — выйти в меню водителя", text)
            self.assertNotIn("/book", text)
            self.assertNotIn("/geo", text)

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
        end_at = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(slot_has_passed(end_at, end_at - timedelta(minutes=1)))
        self.assertTrue(slot_has_passed(end_at, end_at))


if __name__ == "__main__":
    unittest.main()
