import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from cryptography.fernet import Fernet

from group_service import GroupService
from main import MOSCOW_TIMEZONE, format_day, format_week, schedule_week_start
from multiuser import KEYBOARD, create_router, parse_schedule_date
from storage import Group, Store
from test_admin import Session
from university import Lesson


TODAY = date(2026, 10, 25)
GROUP = Group(1, "1241", "39495", "encrypted-fixture", 11, 2, None)


class DateParserTests(unittest.TestCase):
    def test_day_uses_current_month_and_year(self):
        self.assertEqual(parse_schedule_date(" 28 ", TODAY), date(2026, 10, 28))

    def test_month_and_day_use_current_year(self):
        self.assertEqual(parse_schedule_date("1.11", TODAY), date(2026, 11, 1))

    def test_full_date_preserves_explicit_year(self):
        self.assertEqual(parse_schedule_date("01.02.2027", TODAY), date(2027, 2, 1))

    def test_leap_day(self):
        self.assertEqual(parse_schedule_date("29.02.2028", TODAY), date(2028, 2, 29))

    def test_invalid_dates(self):
        for text in ("", "0", "32", "1241", "31.04", "29.02.2027", "28/10", "31.12.9999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_schedule_date(text, TODAY)


class ScheduleWeekTests(unittest.TestCase):
    def test_both_weeks_follow_calendar_with_sunday_rollover(self):
        for current, expected in (
            (date(2026, 10, 24), date(2026, 10, 19)),
            (date(2026, 10, 25), date(2026, 10, 26)),
            (date(2026, 10, 26), date(2026, 10, 26)),
            (date(2026, 10, 28), date(2026, 10, 26)),
            (date(2026, 10, 31), date(2026, 10, 26)),
            (date(2026, 11, 1), date(2026, 11, 2)),
            (date(2026, 12, 27), date(2026, 12, 28)),
            (date(2026, 12, 31), date(2026, 12, 28)),
            (date(2027, 1, 3), date(2027, 1, 4)),
        ):
            with self.subTest(today=current):
                self.assertEqual(schedule_week_start(current), expected)
                self.assertEqual(schedule_week_start(current, next_week=True),
                                 expected + timedelta(days=7))


class ScheduleDateRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = AsyncMock(spec=Store)
        self.store.user_group.return_value = GROUP
        self.store.choose_group.return_value = GROUP
        self.store.new_ticket.return_value = "fixture-ticket"
        self.service = GroupService(self.store, Fernet.generate_key().decode(), "https://bot.example")
        self.service.week = AsyncMock(side_effect=self.week)
        self.session = Session()
        self.bot = Bot("123456789:AA_LOCAL_TEST_TOKEN_12345678901234567890", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.service))
        self.sequence = 0
        self.now = datetime(2026, 10, 25, 23, 30, tzinfo=MOSCOW_TIMEZONE)

    async def asyncTearDown(self):
        await self.dispatcher.storage.close()
        await self.bot.session.close()

    async def week(self, group, day):
        start = day - timedelta(days=day.weekday())
        return {start + timedelta(days=offset): [Lesson(
            "13:00", "14:30", f"Предмет {offset}", "Практика", "101", "Преподаватель",
        )] for offset in range(7)}

    async def send(self, text, user_id=11):
        self.sequence += 1
        self.session.calls.clear()
        update = {"update_id": self.sequence, "message": {
            "message_id": self.sequence, "date": 1792944000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Fixture"}, "text": text,
        }}
        with patch("multiuser.monotonic", return_value=100 + self.sequence * 2), \
                patch("multiuser.datetime") as clock:
            clock.now.side_effect = lambda zone: self.now.astimezone(zone)
            await self.dispatcher.feed_raw_update(self.bot, update)
            if clock.now.called:
                clock.now.assert_called_with(MOSCOW_TIMEZONE)

    def texts(self):
        return "\n".join(call.text for call in self.session.calls if isinstance(call, SendMessage))

    async def test_keyboard_has_both_new_buttons(self):
        labels = [button.text for row in KEYBOARD.keyboard for button in row]
        self.assertIn("На дату", labels)
        self.assertIn("На следующую неделю", labels)

    async def test_prompt_does_not_fetch_or_change_group(self):
        await self.send("На дату")
        self.assertIn("10.2026", self.texts())
        self.service.week.assert_not_awaited()
        self.store.choose_group.assert_not_awaited()

    async def test_day_after_button_fetches_selected_day_only(self):
        await self.send("На дату")
        await self.send("28")
        self.service.week.assert_awaited_once_with(GROUP, date(2026, 10, 28))
        self.assertIn("28.10.2026", self.texts())
        self.assertNotIn("29.10.2026", self.texts())
        self.store.choose_group.assert_not_awaited()

    async def test_full_date_fetches_another_month_and_year(self):
        await self.send("На дату")
        await self.send("01.02.2027")
        self.service.week.assert_awaited_once_with(GROUP, date(2027, 2, 1))
        self.assertIn("01.02.2027", self.texts())

    async def test_invalid_date_keeps_input_mode(self):
        await self.send("На дату")
        await self.send("31.04")
        self.assertIn("Такой даты нет", self.texts())
        self.service.week.assert_not_awaited()
        await self.send("30.04")
        self.service.week.assert_awaited_once_with(GROUP, date(2026, 4, 30))
        self.store.choose_group.assert_not_awaited()

    async def test_group_number_is_not_accepted_as_date(self):
        await self.send("На дату")
        await self.send("1242")
        self.service.week.assert_not_awaited()
        self.store.choose_group.assert_not_awaited()

    async def test_cancel_then_group_selection_works(self):
        await self.send("На дату")
        await self.send("/cancel")
        await self.send("1242")
        self.store.choose_group.assert_awaited_once_with(11, "1242")
        self.service.week.assert_not_awaited()

    async def test_schedule_button_leaves_date_mode(self):
        await self.send("На дату")
        await self.send("На завтра")
        self.service.week.assert_awaited_once_with(GROUP, date(2026, 10, 26))
        await self.send("1242")
        self.store.choose_group.assert_awaited_once_with(11, "1242")

    async def test_other_user_is_not_waiting_for_first_users_date(self):
        await self.send("На дату", user_id=11)
        await self.send("1242", user_id=22)
        self.store.choose_group.assert_awaited_once_with(22, "1242")
        self.service.week.assert_not_awaited()

    async def test_bare_day_after_restart_does_not_change_group(self):
        await self.send("28")
        self.service.week.assert_awaited_once_with(GROUP, date(2026, 10, 28))
        self.store.choose_group.assert_not_awaited()

    async def test_explicit_group_command_allows_short_group_number(self):
        await self.send("/group")
        await self.send("28")
        self.store.choose_group.assert_awaited_once_with(11, "28")
        self.service.week.assert_not_awaited()

    async def test_next_week_on_sunday_monday_and_year_boundary(self):
        for current, expected in ((date(2026, 10, 25), date(2026, 11, 2)),
                                  (date(2026, 10, 26), date(2026, 11, 2)),
                                  (date(2026, 12, 31), date(2027, 1, 4))):
            with self.subTest(today=current):
                self.now = datetime(current.year, current.month, current.day, 23, 30,
                                    tzinfo=MOSCOW_TIMEZONE)
                self.service.week.reset_mock()
                await self.send("На следующую неделю")
                self.service.week.assert_awaited_once_with(GROUP, expected)
                end = expected + timedelta(days=6)
                self.assertIn(f"{expected:%d.%m.%Y}–{end:%d.%m.%Y}", self.texts())

    async def test_current_week_recalculates_without_restarting_bot(self):
        for current, expected in ((date(2026, 10, 24), date(2026, 10, 19)),
                                  (date(2026, 10, 25), date(2026, 10, 26)),
                                  (date(2026, 10, 26), date(2026, 10, 26)),
                                  (date(2026, 11, 1), date(2026, 11, 2))):
            with self.subTest(today=current):
                self.now = datetime(current.year, current.month, current.day, 0, 1,
                                    tzinfo=MOSCOW_TIMEZONE)
                self.service.week.reset_mock()
                await self.send("На неделю")
                self.service.week.assert_awaited_once_with(GROUP, expected)
                end = expected + timedelta(days=6)
                self.assertIn(f"{expected:%d.%m.%Y}–{end:%d.%m.%Y}", self.texts())

    async def test_switch_uses_moscow_midnight_even_while_server_is_saturday(self):
        self.now = datetime(2026, 10, 24, 20, 59, tzinfo=timezone.utc)
        await self.send("На неделю")
        self.service.week.assert_awaited_with(GROUP, date(2026, 10, 19))
        self.now = datetime(2026, 10, 24, 21, 1, tzinfo=timezone.utc)
        await self.send("На неделю")
        self.service.week.assert_awaited_with(GROUP, date(2026, 10, 26))
        await self.send("На следующую неделю")
        self.service.week.assert_awaited_with(GROUP, date(2026, 11, 2))

    async def test_sunday_day_buttons_still_show_exact_date(self):
        await self.send("На сегодня")
        self.service.week.assert_awaited_with(GROUP, date(2026, 10, 25))
        self.assertIn("Сегодня, 25.10.2026", self.texts())
        await self.send("На завтра")
        self.service.week.assert_awaited_with(GROUP, date(2026, 10, 26))
        await self.send("На дату")
        await self.send("25")
        self.service.week.assert_awaited_with(GROUP, date(2026, 10, 25))
        self.assertIn("В этот день, 25.10.2026", self.texts())

    async def test_empty_next_week_has_correct_label(self):
        start = date(2026, 11, 2)
        self.service.week.side_effect = None
        self.service.week.return_value = {start + timedelta(days=i): [] for i in range(7)}
        await self.send("На следующую неделю")
        self.assertIn("02.11.2026–08.11.2026", self.texts())
        self.assertIn("пар нет", self.texts())
        self.assertNotIn("На этой неделе", self.texts())

    async def test_missing_connection_asks_to_connect(self):
        self.store.user_group.return_value = replace(GROUP, credentials=None)
        await self.send("На дату")
        self.assertIn("пока не подключена", self.texts())
        self.service.week.assert_not_awaited()


class WeekCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_week_does_not_reuse_previous_week_and_sunday_day_stays_available(self):
        store = AsyncMock(spec=Store)
        store.get_group.return_value = GROUP
        store.mark_result.return_value = True
        service = GroupService(store, Fernet.generate_key().decode(), "https://bot.example")

        async def fetch(group, start):
            return {start + timedelta(days=i): [] for i in range(7)}

        service._fetch = AsyncMock(side_effect=fetch)
        previous = await service.week(GROUP, date(2026, 10, 24))
        current = await service.week(GROUP, schedule_week_start(TODAY))
        sunday = await service.week(GROUP, TODAY)
        monday = await service.week(GROUP, date(2026, 10, 26))
        self.assertEqual(min(previous), date(2026, 10, 19))
        self.assertEqual(min(current), date(2026, 10, 26))
        self.assertIs(sunday, previous)
        self.assertIs(monday, current)
        self.assertEqual(service._fetch.await_count, 2)


class TeacherFormattingTests(unittest.TestCase):
    def test_red_emoji_in_day_and_week(self):
        for teacher in ("Томашевский И. И.", "ТОМАШЕВСКИЙ", "у Томашевского"):
            with self.subTest(teacher=teacher):
                lesson = Lesson("13:00", "14:30", "Предмет", "Практика", "101", teacher)
                output = "\n".join(format_day(TODAY, [lesson], "Сегодня"))
                self.assertIn("👹 " + teacher, output)
                self.assertNotIn("😈", output)
                schedule = {TODAY + timedelta(days=i): [] for i in range(7)}
                schedule[TODAY] = [lesson]
                self.assertIn("👹 " + teacher, "\n".join(format_week(TODAY, schedule)))

    def test_other_teachers_keep_standard_icon(self):
        lesson = Lesson("13:00", "14:30", "Предмет", "Практика", "101", "Иванов И. И.")
        self.assertIn("👤 Иванов И. И.", "\n".join(format_day(TODAY, [lesson], "Сегодня")))


if __name__ == "__main__":
    unittest.main()
