import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from group_service import ChangedConnection, GroupService
from multiuser import create_router
from storage import Store


ADMIN = 11
KEY = Fernet.generate_key().decode()
GROUP = {"id": 1, "number": "1241", "connected": True, "failure_kind": None,
         "revision": 2, "user_count": 3, "last_success_at": None}


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text).as_(bot)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        if False:
            yield b""


class AdminRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = AsyncMock(spec=Store)
        self.store.admin_groups.return_value = {
            "group_count": 1, "connected_count": 1, "user_count": 3,
            "page": 0, "pages": 1, "groups": [dict(GROUP)],
        }
        self.store.admin_group.return_value = dict(GROUP)
        self.service = GroupService(self.store, KEY, "https://bot.example")
        self.service.admin_reset = AsyncMock()
        self.session = Session()
        self.bot = Bot("123456789:AA_LOCAL_TEST_TOKEN_12345678901234567890", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.service, ADMIN))

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def send(self, text="/admin", user_id=ADMIN, chat_id=None, chat_type="private",
                   callback=None, username="fixture"):
        user = {"id": user_id, "is_bot": False, "first_name": "Fixture", "username": username}
        message = {"message_id": 1, "date": 1792340000,
                   "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
                   "from": user, "text": text}
        if callback:
            update = {"update_id": 1, "callback_query": {"id": "fixture", "chat_instance": "fixture",
                      "from": user, "message": message, "data": callback}}
        else:
            update = {"update_id": 1, "message": message}
        self.session.calls.clear()
        with patch("multiuser.monotonic", return_value=100 + len(text) + len(callback or "")):
            await self.dispatcher.feed_raw_update(self.bot, update)

    def texts(self):
        return "\n".join(call.text for call in self.session.calls if isinstance(call, SendMessage))

    def buttons(self):
        return [button for call in self.session.calls if isinstance(call, SendMessage)
                and isinstance(call.reply_markup, InlineKeyboardMarkup)
                for row in call.reply_markup.inline_keyboard for button in row]

    async def test_owner_sees_counts_and_groups_without_credentials(self):
        await self.send()
        self.assertIn("Пользователей: 3", self.texts())
        self.assertEqual(self.buttons()[0].callback_data, "admin:group:1:0")
        self.assertNotIn("credentials", self.texts())

    async def test_another_user_with_owner_username_is_denied(self):
        await self.send(user_id=12, username="SkinnyPiness")
        self.store.admin_groups.assert_not_awaited()
        self.assertIn("Доступ только", self.texts())

    async def test_disabled_admin_is_closed(self):
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.service))
        await self.send()
        self.store.admin_groups.assert_not_awaited()

    async def test_public_chat_cannot_open_admin(self):
        await self.send(chat_id=-100123, chat_type="supergroup")
        self.store.admin_groups.assert_not_awaited()

    async def test_foreign_callback_cannot_reset(self):
        await self.send(user_id=12, callback="admin:reset:1:2:0")
        self.service.admin_reset.assert_not_awaited()
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert
                            for call in self.session.calls))

    async def test_owner_callback_in_other_chat_cannot_reset(self):
        await self.send(chat_id=12, callback="admin:reset:1:2:0")
        self.service.admin_reset.assert_not_awaited()

    async def test_detail_does_not_reset(self):
        await self.send(callback="admin:group:1:0")
        self.service.admin_reset.assert_not_awaited()
        self.assertIn("Пользователей: 3", self.texts())
        self.assertEqual(self.buttons()[0].callback_data, "admin:ask:1:2:0")

    async def test_reset_requires_confirmation_button(self):
        await self.send(callback="admin:ask:1:2:0")
        self.service.admin_reset.assert_not_awaited()
        self.assertIn("Сбросить подключение группы 1241?", self.texts())
        self.assertEqual(self.buttons()[0].callback_data, "admin:reset:1:2:0")

    async def test_old_confirmation_does_not_offer_reset(self):
        self.store.admin_group.return_value = {**GROUP, "revision": 3}
        await self.send(callback="admin:ask:1:2:0")
        self.service.admin_reset.assert_not_awaited()
        self.assertEqual(self.buttons(), [])

    async def test_confirmed_reset_passes_expected_revision(self):
        await self.send(callback="admin:reset:1:2:0")
        self.service.admin_reset.assert_awaited_once_with(1, 2)
        self.assertIn("Подключение сброшено", self.texts())
        self.assertTrue(any(isinstance(call, EditMessageReplyMarkup) for call in self.session.calls))

    async def test_cancel_removes_confirmation_without_reset(self):
        await self.send(callback="admin:cancel:1:0")
        self.service.admin_reset.assert_not_awaited()
        self.assertTrue(any(isinstance(call, EditMessageReplyMarkup) for call in self.session.calls))

    async def test_stale_reset_reports_change(self):
        self.service.admin_reset.side_effect = ChangedConnection("Подключение уже изменилось")
        await self.send(callback="admin:reset:1:2:0")
        self.assertIn("уже изменилось", self.texts())
        self.assertNotIn("✅", self.texts())

    async def test_unconnected_group_has_no_reset_button(self):
        self.store.admin_group.return_value = {**GROUP, "connected": False}
        await self.send(callback="admin:group:1:0")
        self.assertFalse(any("ask:" in (button.callback_data or "") for button in self.buttons()))

    async def test_pagination_reloads_requested_page(self):
        await self.send(callback="admin:page:2")
        self.store.admin_groups.assert_awaited_once_with(2)

    async def test_malformed_callback_does_not_access_store(self):
        await self.send(callback="admin:reset:1:not-a-revision:0")
        self.store.admin_group.assert_not_awaited()
        self.service.admin_reset.assert_not_awaited()


class AdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_clears_only_target_group_cache(self):
        store = AsyncMock(spec=Store)
        store.admin_reset.return_value = True
        service = GroupService(store, KEY, "https://bot.example")
        service.cache[(1, 2, "week")] = (999, {})
        service.cache[(2, 2, "week")] = (999, {})
        await service.admin_reset(1, 2)
        self.assertEqual(list(service.cache), [(2, 2, "week")])

    async def test_stale_reset_preserves_cache(self):
        store = AsyncMock(spec=Store)
        store.admin_reset.return_value = False
        service = GroupService(store, KEY, "https://bot.example")
        service.cache[(1, 3, "week")] = (999, {})
        with self.assertRaises(ChangedConnection):
            await service.admin_reset(1, 2)
        self.assertEqual(len(service.cache), 1)


class AdminConfigurationTests(unittest.TestCase):
    def test_invalid_administrator_ids_fail_closed(self):
        from cloud import CloudSettings
        for value in ("@SkinnyPiness", "0", "-11", "11,12", "1e8"):
            with self.subTest(value=value), patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": value}, clear=True):
                with self.assertRaises(SystemExit):
                    CloudSettings.from_env()


class TransactionStore(Store):
    @asynccontextmanager
    async def connection(self):
        yield self.fixture_connection


@unittest.skipUnless(os.getenv("RUN_NEON_TESTS") == "1", "Нужна тестовая связь с Neon")
class AdminDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        real = Store(dotenv_values(".env.neon")["DATABASE_URL"])
        self.manager = real.connection()
        self.connection = await self.manager.__aenter__()
        self.transaction = self.connection.transaction(force_rollback=True)
        await self.transaction.__aenter__()
        self.store = TransactionStore("fixture")
        self.store.fixture_connection = self.connection
        self.group = await self.store.choose_group(900000000000091, "999999999991")
        await self.store.attach(self.group, "fixture-group", "encrypted-fixture", 900000000000091)
        self.group = await self.store.get_group(self.group.id)

    async def asyncTearDown(self):
        await self.transaction.__aexit__(None, None, None)
        await self.manager.__aexit__(None, None, None)

    async def test_admin_summary_contains_no_credentials(self):
        summary = await self.store.admin_group(self.group.id)
        self.assertEqual(summary["user_count"], 1)
        self.assertTrue(summary["connected"])
        self.assertNotIn("credentials", summary)
        overview = await self.store.admin_groups(10**6)
        self.assertEqual(overview["page"], overview["pages"] - 1)
        self.assertTrue(any(item["id"] == self.group.id for item in overview["groups"]))

    async def test_reset_preserves_membership_and_protects_reconnected_account(self):
        self.assertTrue(await self.store.admin_reset(self.group.id, self.group.revision))
        empty = await self.store.get_group(self.group.id)
        self.assertIsNone(empty.credentials)
        self.assertIsNone(empty.owner_id)
        self.assertEqual((await self.store.user_group(900000000000091)).id, empty.id)
        self.assertTrue(await self.store.attach(empty, "new-group", "new-cipher", 900000000000091))
        self.assertFalse(await self.store.admin_reset(self.group.id, self.group.revision))
        self.assertEqual((await self.store.get_group(self.group.id)).credentials, "new-cipher")


if __name__ == "__main__":
    unittest.main()
