import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

from main import MAIN_KEYBOARD, PROJECT_DIR, format_day, router
from reminders import MorningReminders
from university import UniversityClient


WEBHOOK_PATH = "/telegram"
cloud_commands = Router(name="cloud_commands")


@dataclass(frozen=True)
class CloudSettings:
    token: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    secret: str = field(repr=False)
    base_url: str
    port: int = 10000

    @classmethod
    def from_env(cls) -> "CloudSettings":
        required = (
            "BOT_TOKEN", "UNIVERSITY_USERNAME",
            "UNIVERSITY_PASSWORD", "WEBHOOK_SECRET",
        )
        values = {name: os.getenv(name, "") for name in required}
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise SystemExit("Добавь переменные окружения: " + ", ".join(missing))

        secret = values["WEBHOOK_SECRET"].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret) is None:
            raise SystemExit(
                "WEBHOOK_SECRET должен содержать от 32 до 256 символов: "
                "латинские буквы, цифры, дефис или подчёркивание."
            )

        base_url = (
            os.getenv("WEBHOOK_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        )
        try:
            url = urlsplit(base_url)
            valid_url = (
                url.scheme == "https"
                and url.hostname
                and not url.username
                and not url.password
                and url.path in ("", "/")
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise SystemExit(
                "Нужен HTTPS-адрес сервера в WEBHOOK_BASE_URL или RENDER_EXTERNAL_URL."
            )

        try:
            port = int(os.getenv("PORT", "10000"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise SystemExit("PORT должен быть числом от 1 до 65535.") from None

        return cls(
            token=values["BOT_TOKEN"].strip(),
            username=values["UNIVERSITY_USERNAME"],
            password=values["UNIVERSITY_PASSWORD"],
            secret=secret,
            base_url=base_url.rstrip("/"),
            port=port,
        )


@cloud_commands.message(CommandStart())
async def cloud_start_handler(message: Message) -> None:
    await message.answer(
        "✅ Бот подключён!\n\n"
        "Я показываю расписание группы 1241.\n"
        "Кнопки управления расположены под полем ввода сообщения.\n\n"
        "☀️ Утренняя рассылка выключена.",
        reply_markup=MAIN_KEYBOARD,
    )


@cloud_commands.message(Command("test_morning"))
async def cloud_test_handler(message: Message, reminders: MorningReminders) -> None:
    if message.chat.type != "private":
        await message.answer("Отправь эту команду в личном чате с ботом.")
        return
    await message.answer(
        "Сейчас отправлю проверочное утреннее сообщение.\n"
        "Автоматическая рассылка остаётся выключенной.",
        reply_markup=MAIN_KEYBOARD,
    )
    await reminders.send_test(message.chat.id)


@cloud_commands.message(Command("stop"))
async def cloud_stop_handler(message: Message) -> None:
    await message.answer(
        "Утренняя рассылка выключена.",
        reply_markup=MAIN_KEYBOARD,
    )


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app(settings: CloudSettings) -> web.Application:
    bot = Bot(token=settings.token)
    schedule_client = UniversityClient(settings.username, settings.password)
    reminders = MorningReminders(
        bot=bot,
        schedule_client=schedule_client,
        formatter=format_day,
        storage_path=PROJECT_DIR / "data" / "morning_subscribers.json",
    )
    dispatcher = Dispatcher()
    dispatcher.include_routers(cloud_commands, router)
    dispatcher["schedule_client"] = schedule_client
    dispatcher["reminders"] = reminders

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    async def resources(application: web.Application):
        try:
            await bot.set_webhook(
                url=settings.base_url + WEBHOOK_PATH,
                secret_token=settings.secret,
                allowed_updates=dispatcher.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            logging.info("Бот запущен в режиме webhook. Утренняя рассылка выключена.")
            yield
        finally:
            await schedule_client.close()
            await bot.session.close()

    app.cleanup_ctx.append(resources)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.secret,
        handle_in_background=False,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    return app


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    settings = CloudSettings.from_env()
    web.run_app(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        access_log=None,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
