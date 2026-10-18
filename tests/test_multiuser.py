import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, Message
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from account_page import register_account_pages
from group_service import ChangedConnection, GroupService
from multiuser import create_router
from main import MOSCOW_TIMEZONE
from storage import Group, StorageError, Store
from university import AccountError, ScheduleError, normalize_group, parse_schedule


KEY = Fernet.generate_key().decode()
TODAY = date(2026, 10, 19)
GROUP = Group(1, "1241", "39495", "encrypted", 11, 2, None)


class ParserTests(unittest.TestCase):
    def test_group_normalization(self):
        self.assertEqual(normalize_group(" 1241 "), "1241")
        with self.assertRaises(ValueError):
            normalize_group("<script>")

    def test_login_page_is_not_empty_schedule(self):
        with self.assertRaises(ScheduleError):
            parse_schedule('<form><input type="password">Вход</form>', TODAY, TODAY)

    def test_empty_day_is_valid(self):
        self.assertEqual(parse_schedule("", TODAY, TODAY), {TODAY: []})


class FixtureStore(Store):
    @asynccontextmanager
    async def connection(self):
        yield self.fixture_connection


class FakeUniversity:
    def __init__(self, fixture, username, password, group_id="39495"):
        self.fixture = fixture
        self.group_id = group_id

    async def available_groups(self):
        return {"1241": "39495"}

    async def get_period(self, start, end):
        self.fixture.fetches += 1
        await asyncio.sleep(0)
        if self.fixture.source_error:
            raise self.fixture.source_error
        return {start + timedelta(days=offset): [] for offset in range((end - start).days + 1)}

    async def close(self):
        pass


@unittest.skipUnless(os.getenv("RUN_NEON_TESTS") == "1", "Нужна тестовая связь с Neon")
class DatabaseAndServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        values = dotenv_values(".env.neon")
        self.real_store = Store(values["DATABASE_URL"])
        self.connection_manager = self.real_store.connection()
        self.connection = await self.connection_manager.__aenter__()
        self.transaction = self.connection.transaction(force_rollback=True)
        await self.transaction.__aenter__()
        self.store = FixtureStore(values["DATABASE_URL"])
        self.store.fixture_connection = self.connection
        self.group = await self.store.choose_group(900000000000011, "1241")
        self.fetches = 0
        self.source_error = None
        self.service = GroupService(
            self.store, KEY, "https://bot.example",
            client_factory=lambda *args, **kwargs: FakeUniversity(self, *args, **kwargs),
        )

    async def asyncTearDown(self):
        await self.transaction.__aexit__(None, None, None)
        await self.connection_manager.__aexit__(None, None, None)

    async def connected(self):
        value = self.service.encrypt("fixture-student", "fixture-password")
        self.assertTrue(await self.store.attach(self.group, "39495", value, 900000000000011))
        return await self.store.get_group(self.group.id)

    async def test_group_shared_by_students(self):
        peer = await self.store.choose_group(900000000000012, "1241")
        self.assertEqual(peer.id, self.group.id)
        self.assertEqual((await self.store.user_group(900000000000011)).id, peer.id)

    async def test_tickets_bound_to_user_group_and_expiry(self):
        token = await self.store.new_ticket(900000000000011, self.group)
        self.assertIsNotNone(await self.store.get_ticket(token))
        await self.store.choose_group(900000000000011, "9999")
        self.assertIsNone(await self.store.get_ticket(token))

    async def test_new_ticket_invalidates_previous(self):
        old = await self.store.new_ticket(900000000000011, self.group)
        new = await self.store.new_ticket(900000000000011, self.group)
        self.assertIsNone(await self.store.get_ticket(old))
        self.assertIsNotNone(await self.store.get_ticket(new))

    async def test_expired_ticket(self):
        token = await self.store.new_ticket(900000000000011, self.group)
        await self.connection.execute("UPDATE schedule_bot.login_tickets SET expires_at = now() - interval '1 second' WHERE telegram_id = 900000000000011")
        self.assertIsNone(await self.store.get_ticket(token))

    async def test_login_limit_survives_new_link(self):
        token = await self.store.new_ticket(900000000000011, self.group)
        for _ in range(5):
            self.assertIsNotNone(await self.store.claim_attempt(token))
        token = await self.store.new_ticket(900000000000011, self.group)
        with self.assertRaises(StorageError):
            await self.store.claim_attempt(token)

    async def test_credentials_encrypted_and_hidden_from_repr(self):
        group = await self.connected()
        self.assertNotIn("fixture-password", group.credentials)
        self.assertNotIn(group.credentials, repr(group))
        self.assertEqual(self.service.decrypt(group)["username"], "fixture-student")
        other = GroupService(self.store, Fernet.generate_key().decode(), "https://bot.example")
        with self.assertRaises(ScheduleError):
            other.decrypt(group)

    async def test_attach_rejects_wrong_group(self):
        self.group = await self.store.choose_group(900000000000011, "9999")
        ticket = {"group_id": self.group.id, "revision": self.group.revision,
                  "telegram_id": 900000000000011}
        with self.assertRaises(ScheduleError):
            await self.service.attach(ticket, "fixture", "fixture")
        self.assertIsNone((await self.store.get_group(self.group.id)).credentials)

    async def test_attach_only_once(self):
        ticket = {"group_id": self.group.id, "revision": self.group.revision,
                  "telegram_id": 900000000000011}
        await self.service.attach(ticket, "fixture", "fixture")
        with self.assertRaises(ChangedConnection):
            await self.service.attach(ticket, "someone-else", "fixture")

    async def test_concurrent_students_fetch_once_and_empty_is_success(self):
        group = await self.connected()
        results = await asyncio.gather(*(self.service.week(group, TODAY) for _ in range(8)))
        self.assertEqual(self.fetches, 1)
        self.assertTrue(all(not any(schedule.values()) for schedule in results))
        self.assertIsNone((await self.store.get_group(group.id)).failure_kind)

    async def test_site_error_does_not_allow_reset(self):
        group = await self.connected()
        self.source_error = ScheduleError("Временная ошибка сайта")
        with self.assertRaises(ScheduleError):
            await self.service.week(group, TODAY)
        with self.assertRaises(ChangedConnection):
            await self.service.reset(900000000000011, group.id, group.revision, TODAY)

    async def test_auth_failure_stops_repeated_logins_and_allows_reset(self):
        group = await self.connected()
        self.source_error = AccountError("Пароль отклонён")
        for _ in range(2):
            with self.assertRaises(AccountError):
                await self.service.week(group, TODAY)
        self.assertEqual(self.fetches, 1)
        self.assertTrue(await self.service.reset(900000000000011, group.id, group.revision, TODAY))
        fresh = await self.store.get_group(group.id)
        self.assertIsNone(fresh.credentials)
        self.assertGreater(fresh.revision, group.revision)

    async def test_recovered_account_is_not_reset(self):
        group = await self.connected()
        await self.store.mark_result(group, failed=True)
        self.assertFalse(await self.service.reset(900000000000011, group.id, group.revision, TODAY))
        self.assertIsNotNone((await self.store.get_group(group.id)).credentials)

    async def test_other_group_cannot_reset(self):
        group = await self.connected()
        await self.store.mark_result(group, failed=True)
        await self.store.choose_group(900000000000012, "9999")
        with self.assertRaises(ChangedConnection):
            await self.service.reset(900000000000012, group.id, group.revision, TODAY)
        self.assertFalse(await self.store.reset(900000000000012, group))

    async def test_old_button_cannot_reset_reconnected_group(self):
        group = await self.connected()
        await self.store.mark_result(group, failed=True)
        self.assertTrue(await self.store.reset(900000000000011, group))
        empty = await self.store.get_group(group.id)
        await self.store.attach(empty, "39495", self.service.encrypt("new", "new"), 900000000000011)
        self.assertFalse(await self.store.reset(900000000000011, group))

    async def test_owner_can_revoke_but_peer_cannot(self):
        group = await self.connected()
        await self.store.choose_group(900000000000012, "1241")
        self.assertFalse(await self.service.disconnect_owner(900000000000012))
        self.assertTrue(await self.service.disconnect_owner(900000000000011))


class AccountPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = AsyncMock(spec=Store)
        store.get_ticket.return_value = {"number": "1241", "group_id": 1, "revision": 1, "telegram_id": 11}
        store.claim_attempt.return_value = store.get_ticket.return_value
        self.service = GroupService(store, KEY, "https://bot.example")
        self.service.attach = AsyncMock()
        app = web.Application()
        register_account_pages(app, self.service)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_form_security_and_consent(self):
        response = await self.client.get("/connect?token=fixture")
        content = await response.text()
        self.assertIn('type="password"', content)
        self.assertIn('name="consent"', content)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_expired_link(self):
        self.service.store.get_ticket.return_value = None
        self.assertEqual((await self.client.get("/connect?token=expired")).status, 410)

    async def test_cross_site_submission_rejected(self):
        response = await self.client.post("/connect", data={"token": "fixture"}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status, 403)
        self.service.attach.assert_not_awaited()

    async def test_no_consent_no_login(self):
        response = await self.client.post("/connect", data={"token": "fixture", "username": "test", "password": "private-fixture"})
        self.assertNotIn("private-fixture", await response.text())
        self.service.attach.assert_not_awaited()

    async def test_password_not_reflected_on_failure(self):
        self.service.attach.side_effect = AccountError("Пароль отклонён")
        response = await self.client.post("/connect", data={"token": "fixture", "username": "test", "password": "private-fixture", "consent": "yes"})
        content = await response.text()
        self.assertIn("Пароль отклонён", content)
        self.assertNotIn("private-fixture", content)

    async def test_success(self):
        response = await self.client.post("/connect", data={"token": "fixture", "username": "test", "password": "fixture", "consent": "yes"})
        self.assertIn("Группа подключена", await response.text())
        self.service.attach.assert_awaited_once()


class RecordingSession(BaseSession):
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

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        if False:
            yield b""


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = AsyncMock(spec=Store)
        self.store.user_group.return_value = GROUP
        self.store.new_ticket.return_value = "fixture-ticket"
        self.service = GroupService(self.store, KEY, "https://bot.example")
        self.service.week = AsyncMock()
        self.service.reset = AsyncMock(return_value=True)
        self.session = RecordingSession()
        self.bot = Bot("123456789:AA_LOCAL_TEST_TOKEN_12345678901234567890", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.service))

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def send(self, text):
        update = {"update_id": 1, "message": {"message_id": 1, "date": 1792340000,
                  "chat": {"id": 11, "type": "private"},
                  "from": {"id": 11, "is_bot": False, "first_name": "Test"}, "text": text}}
        with patch("multiuser.monotonic", return_value=100):
            await self.dispatcher.feed_raw_update(self.bot, update)

    def inline_buttons(self):
        return [button for call in self.session.calls if isinstance(call, SendMessage)
                and isinstance(call.reply_markup, InlineKeyboardMarkup)
                for row in call.reply_markup.inline_keyboard for button in row]

    async def test_start_requests_group_for_new_user(self):
        self.store.user_group.return_value = None
        await self.send("/start")
        self.assertTrue(any("номер" in call.text for call in self.session.calls if isinstance(call, SendMessage)))

    async def test_account_error_has_reset_under_message(self):
        self.service.week.side_effect = AccountError("Пароль отклонён")
        await self.send("На сегодня")
        self.assertEqual(self.inline_buttons()[0].callback_data, "reset:1:2")

    async def test_site_error_has_no_reset(self):
        self.service.week.side_effect = ScheduleError("Сайт недоступен")
        await self.send("На сегодня")
        self.assertEqual(self.inline_buttons(), [])

    async def test_empty_day_has_no_reset(self):
        today = datetime.now(MOSCOW_TIMEZONE).date()
        self.service.week.return_value = {today + timedelta(days=offset): [] for offset in range(-1, 8)}
        await self.send("На сегодня")
        self.assertEqual(self.inline_buttons(), [])
        self.assertTrue(any("пар нет" in call.text for call in self.session.calls if isinstance(call, SendMessage)))

    async def test_unconnected_group_has_link_not_reset(self):
        self.store.user_group.return_value = replace(GROUP, credentials=None, university_id=None)
        await self.send("Моя группа")
        self.assertTrue(self.inline_buttons()[0].url.startswith("https://bot.example/connect?token="))
        self.assertIsNone(self.inline_buttons()[0].callback_data)

    async def test_reset_button_uses_pressing_user_for_new_link(self):
        self.store.user_group.return_value = replace(GROUP, credentials=None, university_id=None)
        update = {"update_id": 2, "callback_query": {
            "id": "fixture-callback", "chat_instance": "fixture-chat",
            "from": {"id": 11, "is_bot": False, "first_name": "Test"},
            "data": "reset:1:2", "message": {
                "message_id": 1, "date": 1792340000, "chat": {"id": 11, "type": "private"},
                "from": {"id": self.bot.id, "is_bot": True, "first_name": "Bot"},
                "text": "Ошибка подключения"}}}
        with patch("multiuser.monotonic", return_value=100):
            await self.dispatcher.feed_raw_update(self.bot, update)
        self.service.reset.assert_awaited_once()
        self.store.new_ticket.assert_awaited_once_with(11, self.store.user_group.return_value)
        self.assertTrue(self.inline_buttons()[0].url)

    async def test_commands_in_public_chat_do_not_collect_credentials(self):
        update = {"update_id": 2, "message": {"message_id": 1, "date": 1792340000,
                  "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
                  "from": {"id": 11, "is_bot": False, "first_name": "Test"}, "text": "/start"}}
        await self.dispatcher.feed_raw_update(self.bot, update)
        self.store.user_group.assert_not_awaited()
        self.assertEqual(self.inline_buttons(), [])


class CloudGroupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_config_and_webhook_are_wired(self):
        import cloud
        environment = {
            "BOT_TOKEN": "123456789:AA_LOCAL_TEST_TOKEN_12345678901234567890",
            "WEBHOOK_SECRET": "fixture-secret_" + "x" * 32,
            "RENDER_EXTERNAL_URL": "https://bot.example",
            "DATABASE_URL": "postgresql://fixture:fixture@ep-fixture.neon.tech/fixture",
            "CREDENTIALS_KEY": KEY,
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = cloud.CloudSettings.from_env()
        for field in ("BOT_TOKEN", "WEBHOOK_SECRET", "DATABASE_URL", "CREDENTIALS_KEY"):
            self.assertNotIn(environment[field], repr(settings))
        session = RecordingSession()
        bot = Bot(settings.token, session=session)
        store = AsyncMock(spec=Store)
        store.user_group.return_value = replace(GROUP, credentials=None, university_id=None)
        store.new_ticket.return_value = "fixture-ticket"
        with patch("cloud.Bot", return_value=bot), patch("cloud.Store", return_value=store), \
                patch.object(bot, "set_webhook", new_callable=AsyncMock) as webhook:
            client = TestClient(TestServer(cloud.create_app(settings)))
            try:
                await client.start_server()
                store.initialize.assert_awaited_once()
                self.assertEqual(set(webhook.await_args.kwargs["allowed_updates"]), {"message", "callback_query"})
                self.assertEqual((await client.get("/health")).status, 200)
                self.assertEqual((await client.post("/telegram", json={"update_id": 1})).status, 401)
                update = {"update_id": 1, "message": {"message_id": 1, "date": 1792340000,
                          "chat": {"id": 11, "type": "private"},
                          "from": {"id": 11, "is_bot": False, "first_name": "Test"}, "text": "/start"}}
                response = await client.post("/telegram", json=update,
                                            headers={"X-Telegram-Bot-Api-Secret-Token": settings.secret})
                self.assertEqual(response.status, 200)
                self.assertTrue(any(isinstance(call, SendMessage) for call in session.calls))
                self.assertEqual((await client.get("/connect?token=fixture")).status, 200)
            finally:
                await client.close()

    async def test_incomplete_database_configuration_is_rejected(self):
        import cloud
        for values in ({"DATABASE_URL": "postgresql://fixture@ep-fixture.neon.tech/test"},
                       {"CREDENTIALS_KEY": KEY}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(SystemExit):
                cloud.CloudSettings.from_env()


if __name__ == "__main__":
    unittest.main()
