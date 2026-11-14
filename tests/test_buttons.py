"""Нажатия «В меню» и «Изменить данные» у всех ролей, и устаревший callback."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import create_engine

from dockbot.access import StaffMember
from dockbot.bot.app import build_dispatcher
from dockbot.bot.keyboard import button_labels
from dockbot.bot.wizard import BookFlow
from dockbot.models import StaffRole


class _Session:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def close(self) -> None:
        return None


_ENGINE = create_engine("sqlite://")
_DP = build_dispatcher(_ENGINE)


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = _ENGINE
        self.dp = _DP
        self.bot = Bot(token="123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.session = _Session()
        self.bot.session = self.session
        self.user = User(id=42, is_bot=False, first_name="Алексей")
        self.chat = Chat(id=42, type="private")
        self.message = Message(
            message_id=7,
            date=datetime.now(timezone.utc),
            chat=self.chat,
            from_user=self.user,
            text="шаг",
        )
        self._update_id = 1

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        self.engine.dispose()

    async def test_menu_restores_each_role_keyboard(self) -> None:
        cases = (
            (None, "Записаться"),
            (StaffRole.security, "Сканировать QR"),
            (StaffRole.warehouse, "Монитор смены"),
            (StaffRole.admin, "Ворота"),
        )
        for role, expected in cases:
            with self.subTest(role=role):
                labels = await self._menu(role, state=BookFlow.summary.state)
                self.assertIn(expected, labels)
                self.assertIsNone(await self._state())

    async def test_menu_text_also_restores_the_keyboard(self) -> None:
        labels = await self._menu(StaffRole.security, as_message=True)
        self.assertIn("Сканировать QR", labels)
        self.assertNotIn("Записаться", labels)

    async def test_edit_restarts_plate_from_any_step(self) -> None:
        for state in (BookFlow.summary, BookFlow.plate, BookFlow.document):
            with self.subTest(state=state):
                sent = await self._press(
                    "bk:edit",
                    state=state,
                    data_store={"mode": "create", "slot_ts": 1, "date": "2026-11-15"},
                )
                self.assertTrue(any("Госномер" in item for item in sent))
                self.assertEqual(await self._state(), BookFlow.plate.state)

    async def test_stale_callback_still_says_the_button_expired(self) -> None:
        alerts = await self._alerts("old:button")
        self.assertEqual(alerts, ["Кнопка устарела"])
        alerts = await self._alerts("bk:edit")
        self.assertEqual(alerts, ["Кнопка устарела"])

    async def test_cancel_with_a_dialog_returns_the_role_keyboard(self) -> None:
        labels = await self._press(
            "bk:abort",
            state=BookFlow.plate,
            data_store={"mode": "create"},
            role=StaffRole.warehouse,
            markup=True,
        )
        self.assertIn("Монитор смены", labels)
        self.assertTrue(any("не сохранена" in item for item in labels if isinstance(item, str)))

    async def _menu(self, role, state=None, as_message: bool = False):
        if as_message:
            await self._feed_message("В меню", role=role, state=state)
        else:
            await self._feed("nav:menu", role=role, state=state)
        return self._keyboard_labels()

    async def _press(self, data, state=None, data_store=None, role=None, markup=False):
        await self._feed(data, role=role, state=state, stored=data_store or {})
        texts = [getattr(call, "text", "") or "" for call in self.session.calls]
        if markup:
            texts.extend(self._keyboard_labels())
        return texts

    async def _alerts(self, data: str) -> list[str]:
        await self._feed(data, stored={})
        return [
            call.text
            for call in self.session.calls
            if getattr(call, "show_alert", False) and getattr(call, "text", None)
        ]

    async def _feed(self, data: str, role=None, state=None, stored=None) -> None:
        self._person = role
        self.session.calls.clear()
        key = self._key()
        await self.dp.storage.set_state(key, state)
        await self.dp.storage.set_data(key, {} if stored is None else stored)
        query = CallbackQuery(
            id=f"cb{self._update_id}",
            from_user=self.user,
            chat_instance="ci",
            data=data,
            message=self.message,
        )
        await self._dispatch(Update(update_id=self._update_id, callback_query=query))

    async def _feed_message(self, text: str, role=None, state=None) -> None:
        self._person = role
        self.session.calls.clear()
        key = self._key()
        await self.dp.storage.set_state(key, state)
        await self.dp.storage.set_data(key, {})
        message = self.message.model_copy(update={"text": text})
        await self._dispatch(Update(update_id=self._update_id, message=message))

    async def _dispatch(self, update: Update) -> None:
        from unittest.mock import patch

        def staff(_engine, telegram_id):
            if self._person is None:
                return None
            return StaffMember(
                id=1,
                telegram_id=telegram_id,
                role=self._person,
                display_name="Смена",
            )

        self._update_id += 1
        with (
            patch("dockbot.access.active_staff", staff),
            patch("dockbot.bot.fallback.active_staff", staff),
        ):
            await self.dp.feed_update(self.bot, update)

    def _key(self) -> StorageKey:
        return StorageKey(bot_id=self.bot.id, chat_id=self.chat.id, user_id=self.user.id)

    async def _state(self):
        return await self.dp.storage.get_state(self._key())

    def _keyboard_labels(self) -> list[str]:
        labels: list[str] = []
        for call in self.session.calls:
            markup = getattr(call, "reply_markup", None)
            keyboard = getattr(markup, "keyboard", None)
            if keyboard:
                labels.extend(button.text for row in keyboard for button in row)
        return labels


if __name__ == "__main__":
    unittest.main()
