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
from cryptography.fernet import Fernet

from account_page import register_account_pages
from group_service import GroupService
from main import MAIN_KEYBOARD, PROJECT_DIR, format_day, router
from multiuser import create_router
from reminders import MorningReminders
from storage import Store
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
    database_url: str = field(default="", repr=False)
    credentials_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "CloudSettings":
        database_url = os.getenv("DATABASE_URL", "").strip()
        credentials_key = os.getenv("CREDENTIALS_KEY", "").strip()
        if bool(database_url) != bool(credentials_key):
            raise SystemExit("Для групп нужны обе переменные: DATABASE_URL и CREDENTIALS_KEY.")
        if database_url:
            try:
                database = urlsplit(database_url)
                if (database.scheme not in ("postgres", "postgresql")
                        or not database.hostname or not database.hostname.endswith(".neon.tech")):
                    raise ValueError
                Fernet(credentials_key.encode())
            except (ValueError, UnicodeError):
                raise SystemExit("Проверь DATABASE_URL и CREDENTIALS_KEY; значения не выводятся в журнал.") from None
        required = ("BOT_TOKEN", "WEBHOOK_SECRET")
        if not database_url:
            required += ("UNIVERSITY_USERNAME", "UNIVERSITY_PASSWORD")
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
            username=values.get("UNIVERSITY_USERNAME", ""),
            password=values.get("UNIVERSITY_PASSWORD", ""),
            secret=secret,
            base_url=base_url.rstrip("/"),
            port=port,
            database_url=database_url,
            credentials_key=credentials_key,
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
    dispatcher = Dispatcher()
    schedule_client = None
    service = None
    if settings.database_url:
        service = GroupService(Store(settings.database_url), settings.credentials_key, settings.base_url)
        dispatcher.include_router(create_router(service))
    else:
        schedule_client = UniversityClient(settings.username, settings.password)
        reminders = MorningReminders(
            bot=bot,
            schedule_client=schedule_client,
            formatter=format_day,
            storage_path=PROJECT_DIR / "data" / "morning_subscribers.json",
        )
        dispatcher.include_routers(cloud_commands, router)
        dispatcher["schedule_client"] = schedule_client
        dispatcher["reminders"] = reminders

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    if service is not None:
        register_account_pages(app, service)

    async def resources(application: web.Application):
        try:
            if service is not None:
                await service.store.initialize()
            await bot.set_webhook(
                url=settings.base_url + WEBHOOK_PATH,
                secret_token=settings.secret,
                allowed_updates=dispatcher.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            logging.info("Бот запущен в режиме webhook. Утренняя рассылка выключена.")
            yield
        finally:
            if schedule_client is not None:
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
    load_dotenv(PROJECT_DIR / ".env.neon")
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
