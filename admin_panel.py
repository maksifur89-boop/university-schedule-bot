from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from group_service import ChangedConnection, GroupService
from main import MOSCOW_TIMEZONE


def connection_status(group: dict) -> str:
    if not group["connected"]:
        return "не подключена"
    if group["failure_kind"] == "account":
        return "ошибка входа"
    return "подключена"


def button(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="admin:" + action)


def register_admin_handlers(router: Router, service: GroupService,
                            admin_id: int | None) -> None:
    # Проверяем числовой ID при каждой команде и каждом нажатии кнопки.
    # Имя пользователя не используется для выдачи доступа.
    async def allowed(event: Message | CallbackQuery) -> bool:
        message = event.message if isinstance(event, CallbackQuery) else event
        valid = (admin_id is not None and event.from_user is not None
                 and event.from_user.id == admin_id and isinstance(message, Message)
                 and message.chat.type == "private" and message.chat.id == admin_id)
        if not valid:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ только у администратора.", show_alert=True)
            else:
                await event.answer("Доступ только у администратора.")
        return valid

    async def show_list(message: Message, page: int = 0) -> None:
        overview = await service.store.admin_groups(page)
        rows = [[button(
            f"{group['number']} · {group['user_count']} чел. · {connection_status(group)}",
            f"group:{group['id']}:{overview['page']}",
        )] for group in overview["groups"]]
        navigation = []
        if overview["page"] > 0:
            navigation.append(button("← Назад", f"page:{overview['page'] - 1}"))
        if overview["page"] + 1 < overview["pages"]:
            navigation.append(button("Дальше →", f"page:{overview['page'] + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([button("Обновить", f"page:{overview['page']}")])
        text = (f"🛠 Управление ботом\n\nГрупп: {overview['group_count']}\n"
                f"Подключено: {overview['connected_count']}\n"
                f"Пользователей: {overview['user_count']}\n\n"
                f"Страница {overview['page'] + 1} из {overview['pages']}.\n"
                "Выбери группу." if overview["groups"] else
                "🛠 Управление ботом\n\nПока нет зарегистрированных групп.")
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def show_detail(message: Message, group_id: int, page: int) -> None:
        group = await service.store.admin_group(group_id)
        if group is None:
            await message.answer("Группа больше не найдена. Открой /admin заново.")
            return
        text = (f"Группа {group['number']}\n\nПользователей: {group['user_count']}\n"
                f"Подключение: {connection_status(group)}.")
        if group["last_success_at"]:
            checked = group["last_success_at"].astimezone(MOSCOW_TIMEZONE)
            text += f"\nПоследняя успешная загрузка: {checked:%d.%m.%Y %H:%M} МСК."
        rows = []
        if group["connected"]:
            rows.append([button("Сбросить подключение",
                                f"ask:{group_id}:{group['revision']}:{page}")])
        rows.append([button("К списку групп", f"page:{page}")])
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.message(Command("id"))
    async def own_id(message: Message):
        await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    @router.message(Command("admin"))
    async def panel(message: Message):
        if await allowed(message):
            await show_list(message)

    @router.callback_query(F.data.startswith("admin:"))
    async def navigate(callback: CallbackQuery):
        if not await allowed(callback):
            return
        parts = callback.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        sizes = {"page": 3, "group": 4, "cancel": 4, "ask": 5, "reset": 5}
        if (action not in sizes or len(parts) != sizes[action]
                or any(not value.isascii() or not value.isdigit() or len(value) > 18
                       for value in parts[2:])):
            await callback.answer("Эта кнопка недействительна.", show_alert=True)
            return
        await callback.answer()
        if action == "page":
            await show_list(callback.message, int(parts[2]))
            return
        group_id, page = int(parts[2]), int(parts[-1])
        if action in {"group", "cancel"}:
            if action == "cancel":
                await callback.message.edit_reply_markup(reply_markup=None)
            await show_detail(callback.message, group_id, page)
            return
        revision = int(parts[3])
        if action == "ask":
            group = await service.store.admin_group(group_id)
            if group is None or not group["connected"] or group["revision"] != revision:
                await callback.message.answer("Подключение уже изменилось. Открой группу заново.")
                return
            await callback.message.answer(
                f"Сбросить подключение группы {group['number']}?\n\n"
                "Сохранённые данные входа будут удалены из подключения. "
                "Расписание станет доступно после привязки нового аккаунта.\n"
                "Участники останутся в этой группе.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [button("Да, сбросить", f"reset:{group_id}:{revision}:{page}")],
                    [button("Отмена", f"cancel:{group_id}:{page}")],
                ]),
            )
            return
        try:
            await service.admin_reset(group_id, revision)
        except ChangedConnection as error:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(str(error))
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ Подключение сброшено. Участники могут привязать новый аккаунт.")
        await show_detail(callback.message, group_id, page)
