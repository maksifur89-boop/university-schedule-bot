import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

from reminders import MorningReminders
from university import Lesson, ScheduleError, UniversityClient


PROJECT_DIR = Path(__file__).resolve().parent
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)
# График для высшего образования: https://ieml.ru/sveden/document/Rezhim_zanyat.pdf
BELL_TIMES = (
    ("08:30", "09:50"),
    ("10:00", "11:20"),
    ("11:40", "13:00"),
    ("13:10", "14:30"),
    ("14:50", "16:10"),
    ("16:20", "17:40"),
    ("17:50", "19:10"),
    ("19:15", "20:35"),
)
router = Router(name="start")
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="На сегодня"), KeyboardButton(text="На завтра")],
        [KeyboardButton(text="На неделю")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Кнопки расписания — внизу",
)


@router.message(CommandStart())
async def start_handler(message: Message, reminders: MorningReminders) -> None:
    text = (
        "✅ Бот подключён!\n\n"
        "Я буду показывать расписание группы 1241.\n"
        "Кнопки управления расположены под полем ввода сообщения."
    )
    if message.chat.type == "private":
        reminders.subscribe(message.chat.id)
        text += "\n\n☀️ Утреннее расписание включено: каждый день в 07:30 по Москве.\nОтключить: /stop"
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


@router.message(Command("test_morning"))
async def test_morning_handler(message: Message, reminders: MorningReminders) -> None:
    if message.chat.type != "private":
        await message.answer("Отправь эту команду в личном чате с ботом.")
        return
    reminders.subscribe(message.chat.id)
    await message.answer(
        "Рассылка включена: каждый день в 07:30 по Москве.\n"
        "Сейчас отправлю проверочное утреннее сообщение.",
        reply_markup=MAIN_KEYBOARD,
    )
    await reminders.send_test(message.chat.id)


@router.message(Command("stop"))
async def stop_morning_handler(message: Message, reminders: MorningReminders) -> None:
    if message.chat.type != "private":
        await message.answer("Отправь эту команду в личном чате с ботом.")
        return
    reminders.unsubscribe(message.chat.id)
    await message.answer(
        "Утренняя рассылка выключена. Включить снова: /start",
        reply_markup=MAIN_KEYBOARD,
    )


def format_lesson_blocks(lessons: list[Lesson]) -> list[str]:
    blocks = []
    pair_number = 0
    previous_start = None
    previous_end = None
    for lesson in sorted(lessons, key=lambda item: item.starts_at):
        if lesson.starts_at != previous_start:
            if previous_end is not None:
                for starts_at, ends_at in BELL_TIMES:
                    if previous_end <= starts_at and ends_at <= lesson.starts_at:
                        pair_number += 1
                        blocks.append(
                            f"\n\n🪟 {pair_number}-я пара — окно\n"
                            f"🕒 {starts_at}–{ends_at}"
                        )
            pair_number += 1
        blocks.append(
            f"\n\n📘 {lesson.subject}\n"
            f"🕒 {pair_number}-я пара · {lesson.starts_at}–{lesson.ends_at}\n"
            f"🎓 {lesson.lesson_type}\n"
            f"📍 {lesson.location}\n"
            f"👤 {lesson.teacher}"
        )
        previous_start = lesson.starts_at
        previous_end = max(previous_end or lesson.ends_at, lesson.ends_at)
    return blocks


def format_day(day: date, lessons: list[Lesson], label: str, group: str = "1241") -> list[str]:
    heading = f"📅 {label}, {day:%d.%m.%Y}\nГруппа {group}"
    if not lessons:
        hint = "можно спать дальше!" if label == "Сегодня" else "можно выспаться!"
        return [heading + f"\n\n😴 {label} пар нет — {hint}"]
    messages = []
    current = heading
    for block in format_lesson_blocks(lessons):
        if len(current) + len(block) > 3500:
            messages.append(current)
            current = heading + " (продолжение)"
        current += block
    messages.append(current)
    return messages


def format_week(start: date, schedule: dict[date, list[Lesson]], group: str = "1241") -> list[str]:
    end = start + timedelta(days=6)
    heading = f"📆 Неделя {start:%d.%m.%Y}–{end:%d.%m.%Y}\nГруппа {group}"
    if not any(schedule.values()):
        return [heading + "\n\n😴 На этой неделе пар нет — можно отдыхать!"]
    messages = []
    current = heading
    for offset in range(7):
        day = start + timedelta(days=offset)
        label = WEEKDAYS[day.weekday()]
        lessons = schedule[day]
        if lessons:
            blocks = format_day(day, lessons, label, group)
        else:
            blocks = [f"📅 {label}, {day:%d.%m.%Y}\n😴 Пар нет"]
        for block in blocks:
            if len(current) + len(block) + 2 > 3900:
                messages.append(current)
                current = heading + " (продолжение)"
            current += "\n\n" + block
    messages.append(current)
    return messages


@router.message(F.text.in_({"На сегодня", "На завтра"}))
async def day_schedule_handler(message: Message, schedule_client: UniversityClient) -> None:
    offset = 1 if message.text == "На завтра" else 0
    label = "Завтра" if offset else "Сегодня"
    day = datetime.now(MOSCOW_TIMEZONE).date() + timedelta(days=offset)
    await message.answer("Смотрю расписание на сайте вуза…")
    try:
        lessons = await schedule_client.get_day(day)
    except ScheduleError as error:
        logging.warning("Не удалось загрузить расписание: %s", error)
        await message.answer(
            "Не удалось получить расписание с сайта вуза. Попробуй чуть позже.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    for text in format_day(day, lessons, label):
        await message.answer(text, reply_markup=MAIN_KEYBOARD)


@router.message(F.text == "На неделю")
async def week_schedule_handler(message: Message, schedule_client: UniversityClient) -> None:
    today = datetime.now(MOSCOW_TIMEZONE).date()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    await message.answer("Смотрю расписание на неделю на сайте вуза…")
    try:
        schedule = await schedule_client.get_period(start, end)
    except ScheduleError as error:
        logging.warning("Не удалось загрузить расписание на неделю: %s", error)
        await message.answer(
            "Не удалось получить расписание с сайта вуза. Попробуй чуть позже.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    for text in format_week(start, schedule):
        await message.answer(text, reply_markup=MAIN_KEYBOARD)


async def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()

    if not token:
        raise SystemExit("Вставь токен от @BotFather после BOT_TOKEN= в файле .env.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    schedule_client = UniversityClient(
        username=os.getenv("UNIVERSITY_USERNAME", ""),
        password=os.getenv("UNIVERSITY_PASSWORD", ""),
    )
    dispatcher["schedule_client"] = schedule_client

    try:
        async with Bot(token=token) as bot:
            account = await bot.get_me()
            reminders = MorningReminders(
                bot=bot,
                schedule_client=schedule_client,
                formatter=format_day,
                storage_path=PROJECT_DIR / "data" / "morning_subscribers.json",
            )
            dispatcher["reminders"] = reminders
            reminders.start()
            logging.info(
                "Бот @%s подключён. Отправь ему /start в Telegram.",
                account.username,
            )
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    close_bot_session=False,
                )
            finally:
                reminders.stop()
    except TokenValidationError:
        raise SystemExit("Проверь токен в .env: его формат неверный.") from None
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram отклонил токен. Проверь его в @BotFather.") from None
    except TelegramNetworkError:
        raise SystemExit("Не удалось соединиться с Telegram. Проверь интернет.") from None
    finally:
        await schedule_client.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
