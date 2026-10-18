import asyncio
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from time import monotonic

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from admin_panel import register_admin_handlers
from group_service import ChangedConnection, ConnectionNeeded, GroupService
from main import MOSCOW_TIMEZONE, format_day, format_week
from storage import Group, StorageError
from university import AccountError, ScheduleError


KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="На сегодня"), KeyboardButton(text="На завтра")],
        [KeyboardButton(text="На неделю"), KeyboardButton(text="Моя группа")],
    ], resize_keyboard=True, is_persistent=True,
    input_field_placeholder="Выбери расписание",
)


class UserMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self.recent: OrderedDict[int, float] = OrderedDict()
        self.locks: dict[int, asyncio.Lock] = {}
        self.pending: dict[int, int] = {}

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type != "private":
            await event.answer("Открой личный чат с ботом, чтобы выбрать свою группу.")
            return
        if isinstance(event, CallbackQuery) and (
            not isinstance(event.message, Message) or event.message.chat.type != "private"
            or event.message.chat.id != event.from_user.id
        ):
            await event.answer("Открой личный чат с ботом.", show_alert=True)
            return
        if event.from_user is None:
            return
        user_id = event.from_user.id
        now = monotonic()
        previous = self.recent.get(user_id, 0)
        self.recent[user_id] = now
        self.recent.move_to_end(user_id)
        while len(self.recent) > 4096:
            self.recent.popitem(last=False)
        if now - previous < 1:
            if isinstance(event, CallbackQuery):
                await event.answer("Подожди секунду и попробуй ещё раз.")
            return
        lock = self.locks.setdefault(user_id, asyncio.Lock())
        self.pending[user_id] = self.pending.get(user_id, 0) + 1
        try:
            async with lock:
                return await handler(event, data)
        except StorageError as error:
            if isinstance(event, CallbackQuery):
                await event.message.answer(str(error))
            else:
                await event.answer(str(error))
        except Exception as error:
            logging.error("Ошибка обработки запроса: %s", type(error).__name__)
            target = event.message if isinstance(event, CallbackQuery) else event
            await target.answer("Не удалось обработать запрос. Попробуй чуть позже.")
        finally:
            self.pending[user_id] -= 1
            if self.pending[user_id] == 0:
                self.pending.pop(user_id, None)
                self.locks.pop(user_id, None)


def reset_keyboard(group: Group) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сбросить аккаунт группы",
                             callback_data=f"reset:{group.id}:{group.revision}")
    ]])


async def show_group(message: Message, service: GroupService, telegram_id: int | None = None) -> None:
    user_id = telegram_id if telegram_id is not None else message.from_user.id
    group = await service.store.user_group(user_id)
    if group is None:
        await message.answer("Отправь номер своей учебной группы, например 1241.")
        return
    if group.credentials is None:
        url = await service.connection_url(user_id, group)
        await message.answer(
            f"Группа {group.number} пока не подключена.\n\n"
            "Один из участников может подключить свой кабинет вуза. "
            "После этого расписание станет доступно всем, кто выбрал эту группу.\n\n"
            "Вводи логин и пароль только в форме по кнопке ниже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Подключить аккаунт группы", url=url)
            ]]),
        )
        return
    if group.failure_kind == "account":
        await message.answer(
            f"⚠️ Подключение группы {group.number} не работает.\n"
            "Можно сбросить его и привязать другой аккаунт этой группы.",
            reply_markup=reset_keyboard(group),
        )
        return
    await message.answer(
        f"✅ Твоя группа: {group.number}.\nРасписание доступно по кнопкам внизу.\n\n"
        "Сменить группу: /group\nУтренняя рассылка выключена.", reply_markup=KEYBOARD,
    )


def create_router(service: GroupService, admin_id: int | None = None) -> Router:
    router = Router(name="groups")
    middleware = UserMiddleware()
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    register_admin_handlers(router, service, admin_id)

    @router.message(CommandStart())
    async def start(message: Message):
        await message.answer("Привет! Я помогу с расписанием твоей группы.", reply_markup=KEYBOARD)
        await show_group(message, service)

    @router.message(Command("group"))
    async def choose(message: Message):
        await message.answer("Отправь номер учебной группы, например 1241.")

    @router.message(F.text == "Моя группа")
    async def current(message: Message):
        await show_group(message, service)

    @router.message(Command("disconnect"))
    async def disconnect(message: Message):
        if await service.disconnect_owner(message.from_user.id):
            await message.answer("Твоё подключение удалено. Участники группы смогут привязать новый аккаунт.")
        else:
            await message.answer("У выбранной группы нет подключения, которое принадлежит тебе.")

    @router.message(Command("stop"))
    async def stop(message: Message):
        await message.answer("Утренняя рассылка выключена.", reply_markup=KEYBOARD)

    async def send_schedule(message: Message, action: str):
        group = await service.store.user_group(message.from_user.id)
        if group is None or group.credentials is None:
            await show_group(message, service)
            return
        day = datetime.now(MOSCOW_TIMEZONE).date()
        if action == "На завтра":
            day += timedelta(days=1)
        await message.answer("Смотрю расписание…")
        try:
            schedule = await service.week(group, day)
        except AccountError:
            await message.answer(
                f"⚠️ Не удалось войти в кабинет для группы {group.number}.\n"
                "Можно сбросить подключение и привязать рабочий аккаунт.",
                reply_markup=reset_keyboard(group),
            )
            return
        except ConnectionNeeded:
            await show_group(message, service)
            return
        except (ScheduleError, ChangedConnection):
            await message.answer(
                "Не удалось загрузить расписание. Попробуй чуть позже.\n"
                "Если сайт вуза временно недоступен, подключение группы сохраняется.",
                reply_markup=KEYBOARD,
            )
            return
        if action == "На неделю":
            start_day = day - timedelta(days=day.weekday())
            messages = format_week(start_day, schedule, group.number)
        else:
            label = "Завтра" if action == "На завтра" else "Сегодня"
            messages = format_day(day, schedule[day], label, group.number)
        for text in messages:
            await message.answer(text, reply_markup=KEYBOARD)

    @router.message(F.text.in_({"На сегодня", "На завтра", "На неделю"}))
    async def schedule(message: Message):
        await send_schedule(message, message.text)

    @router.message(Command("test_morning"))
    async def morning(message: Message):
        await message.answer("🧪 Проверка утреннего сообщения. Автоматическая рассылка выключена.")
        await send_schedule(message, "На сегодня")

    @router.callback_query(F.data.regexp(r"^reset:\d+:\d+$"))
    async def reset(callback: CallbackQuery):
        await callback.answer("Проверяю подключение…")
        _, group_id, revision = callback.data.split(":")
        try:
            changed = await service.reset(
                callback.from_user.id, int(group_id), int(revision),
                datetime.now(MOSCOW_TIMEZONE).date(),
            )
        except ChangedConnection as error:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(str(error))
            return
        except ScheduleError:
            await callback.message.answer(
                "Сейчас сайт вуза не позволяет проверить аккаунт. Попробуй позже; подключение сохранено."
            )
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        if changed:
            await callback.message.answer("Подключение сброшено. Теперь можно привязать другой аккаунт.")
            await show_group(callback.message, service, callback.from_user.id)
        else:
            await callback.message.answer("✅ Подключение снова работает. Нажми нужную кнопку расписания.")

    @router.message(F.text)
    async def group_number(message: Message):
        text = message.text.strip()
        if not re.fullmatch(r"\d{2,12}(?:[-/][\w]{1,16})?", text):
            await message.answer("Выбери кнопку расписания или отправь номер группы, например 1241.")
            return
        group = await service.store.choose_group(message.from_user.id, text)
        await message.answer(f"Выбрана группа {group.number}.", reply_markup=KEYBOARD)
        await show_group(message, service)

    return router
