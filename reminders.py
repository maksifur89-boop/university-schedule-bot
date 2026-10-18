import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from university import Lesson, ScheduleError, UniversityClient


MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


class MorningReminders:
    def __init__(
        self,
        bot: Bot,
        schedule_client: UniversityClient,
        formatter: Callable[[date, list[Lesson], str], list[str]],
        storage_path: Path,
    ) -> None:
        self.bot = bot
        self.schedule_client = schedule_client
        self.formatter = formatter
        self.storage_path = storage_path
        self.subscribers = self.load_subscribers()
        self.scheduler = AsyncIOScheduler(timezone=MOSCOW_TIMEZONE)

    def load_subscribers(self) -> set[int]:
        if not self.storage_path.exists():
            return set()
        try:
            values = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(values, list) or any(type(value) is not int for value in values):
                raise ValueError
            return set(values)
        except (ValueError, OSError):
            raise SystemExit("Не удалось прочитать файл подписчиков утренней рассылки.") from None

    def save_subscribers(self, subscribers: set[int]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.storage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(subscribers)), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.storage_path)
        self.subscribers = subscribers

    def subscribe(self, chat_id: int) -> None:
        if chat_id not in self.subscribers:
            self.save_subscribers(self.subscribers | {chat_id})

    def unsubscribe(self, chat_id: int) -> None:
        if chat_id in self.subscribers:
            self.save_subscribers(self.subscribers - {chat_id})

    def start(self) -> None:
        self.scheduler.add_job(
            self.broadcast,
            CronTrigger(hour=7, minute=30, timezone=MOSCOW_TIMEZONE),
            id="morning_schedule",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        job = self.scheduler.get_job("morning_schedule")
        logging.info("Следующая утренняя рассылка: %s", job.next_run_time.isoformat())

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def build_messages(self, prefix: str) -> list[str]:
        day = datetime.now(MOSCOW_TIMEZONE).date()
        try:
            lessons = await self.schedule_client.get_day(day)
        except ScheduleError as error:
            logging.warning("Не удалось загрузить утреннее расписание: %s", error)
            return [
                prefix + "\n\nНе удалось загрузить расписание с сайта вуза. "
                "Попробуй позже кнопку «На сегодня»."
            ]
        messages = self.formatter(day, lessons, "Сегодня")
        messages[0] = prefix + "\n\n" + messages[0]
        return messages

    async def send_messages(self, chat_id: int, messages: list[str]) -> bool:
        try:
            for text in messages:
                try:
                    await self.bot.send_message(chat_id, text)
                except TelegramRetryAfter as error:
                    await asyncio.sleep(error.retry_after)
                    await self.bot.send_message(chat_id, text)
        except TelegramForbiddenError:
            self.unsubscribe(chat_id)
            logging.info("Утренняя рассылка отключена: пользователь заблокировал бота.")
            return False
        except TelegramAPIError as error:
            logging.warning("Ошибка отправки утреннего расписания: %s", type(error).__name__)
            return False
        return True

    async def broadcast(self) -> None:
        recipients = sorted(self.subscribers)
        if not recipients:
            return
        messages = await self.build_messages("☀️ Доброе утро!")
        for chat_id in recipients:
            if chat_id in self.subscribers:
                await self.send_messages(chat_id, messages)

    async def send_test(self, chat_id: int) -> None:
        messages = await self.build_messages("🧪 Проверка утренней рассылки")
        await self.send_messages(chat_id, messages)
