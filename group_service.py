import asyncio
import json
from collections import OrderedDict
from datetime import date, timedelta
from time import monotonic

from cryptography.fernet import Fernet, InvalidToken

from storage import Group, Store
from university import AccountError, Lesson, ScheduleError, UniversityClient


class ConnectionNeeded(Exception):
    pass


class ChangedConnection(Exception):
    pass


class GroupService:
    def __init__(self, store: Store, key: str, base_url: str,
                 client_factory=UniversityClient) -> None:
        self.store = store
        self.cipher = Fernet(key.encode())
        self.base_url = base_url
        self.client_factory = client_factory
        self.site_slots = asyncio.Semaphore(2)
        self.locks: dict[int, asyncio.Lock] = {}
        self.cache: OrderedDict[tuple, tuple[float, dict[date, list[Lesson]]]] = OrderedDict()

    def lock(self, group_id: int) -> asyncio.Lock:
        return self.locks.setdefault(group_id, asyncio.Lock())

    def encrypt(self, username: str, password: str) -> str:
        value = json.dumps({"username": username, "password": password}, ensure_ascii=False)
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, group: Group) -> dict[str, str]:
        try:
            value = json.loads(self.cipher.decrypt(group.credentials.encode()).decode())
            if not isinstance(value["username"], str) or not isinstance(value["password"], str):
                raise ValueError
            return value
        except (InvalidToken, ValueError, KeyError, AttributeError, TypeError):
            raise ScheduleError("Не удалось расшифровать подключение. Сообщи администратору бота.") from None

    async def connection_url(self, telegram_id: int, group: Group) -> str:
        token = await self.store.new_ticket(telegram_id, group)
        return f"{self.base_url}/connect?token={token}"

    async def attach(self, ticket: dict, username: str, password: str) -> None:
        async with self.lock(ticket["group_id"]):
            group = await self.store.get_group(ticket["group_id"])
            if group is None or group.revision != ticket["revision"] or group.credentials:
                raise ChangedConnection("Кто-то уже подключил группу. Вернись в бот.")
            async with self.site_slots:
                client = self.client_factory(username, password)
                try:
                    async with asyncio.timeout(45):
                        groups = await client.available_groups()
                        identifier = groups.get(group.number)
                        if identifier is None:
                            raise ScheduleError("В этом кабинете нет выбранной группы. Проверь её номер.")
                        client.group_id = identifier
                        today = date.today()
                        await client.get_period(today, today + timedelta(days=6))
                except TimeoutError:
                    raise ScheduleError("Сайт вуза отвечает слишком долго. Попробуй позже.") from None
                finally:
                    await client.close()
            saved = await self.store.attach(
                group, identifier, self.encrypt(username, password), ticket["telegram_id"]
            )
            if not saved:
                raise ChangedConnection("Подключение уже изменилось. Вернись в бот.")

    async def _fetch(self, group: Group, start: date) -> dict[date, list[Lesson]]:
        credentials = self.decrypt(group)
        async with self.site_slots:
            client = self.client_factory(**credentials, group_id=group.university_id)
            try:
                async with asyncio.timeout(45):
                    return await client.get_period(start, start + timedelta(days=6))
            except TimeoutError:
                raise ScheduleError("Сайт вуза отвечает слишком долго. Попробуй позже.") from None
            finally:
                await client.close()

    async def week(self, group: Group, day: date) -> dict[date, list[Lesson]]:
        start = day - timedelta(days=day.weekday())
        async with self.lock(group.id):
            current = await self.store.get_group(group.id)
            if current is None or current.revision != group.revision:
                raise ChangedConnection("Подключение группы изменилось. Повтори запрос.")
            if not current.credentials:
                raise ConnectionNeeded
            if current.failure_kind == "account":
                raise AccountError("Подключение группы требует восстановления.")
            cache_key = (group.id, group.revision, start)
            cached = self.cache.get(cache_key)
            if cached and cached[0] > monotonic() and current.failure_kind is None:
                self.cache.move_to_end(cache_key)
                return cached[1]
            try:
                schedule = await self._fetch(current, start)
            except AccountError:
                await self.store.mark_result(current, failed=True)
                self.cache.pop(cache_key, None)
                raise
            if not await self.store.mark_result(current, failed=False):
                raise ChangedConnection("Подключение группы изменилось. Повтори запрос.")
            self.cache[cache_key] = (monotonic() + 600, schedule)
            self.cache.move_to_end(cache_key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return schedule

    async def reset(self, telegram_id: int, group_id: int, revision: int, day: date) -> bool:
        async with self.lock(group_id):
            group = await self.store.user_group(telegram_id)
            if (group is None or group.id != group_id or group.revision != revision
                    or group.failure_kind != "account" or not group.credentials):
                raise ChangedConnection("Эта кнопка устарела. Открой «Моя группа».")
            start = day - timedelta(days=day.weekday())
            try:
                schedule = await self._fetch(group, start)
            except AccountError:
                if not await self.store.reset(telegram_id, group):
                    raise ChangedConnection("Подключение уже изменилось. Открой «Моя группа».")
                for key in list(self.cache):
                    if key[0] == group_id:
                        del self.cache[key]
                return True
            if not await self.store.mark_result(group, failed=False):
                raise ChangedConnection("Подключение уже изменилось. Повтори запрос.")
            self.cache[(group.id, group.revision, start)] = (monotonic() + 600, schedule)
            return False

    async def disconnect_owner(self, telegram_id: int) -> bool:
        group = await self.store.user_group(telegram_id)
        if group is None:
            return False
        async with self.lock(group.id):
            changed = await self.store.disconnect_owner(telegram_id, group)
            if changed:
                for key in list(self.cache):
                    if key[0] == group.id:
                        del self.cache[key]
            return changed

    async def admin_reset(self, group_id: int, revision: int) -> None:
        async with self.lock(group_id):
            if not await self.store.admin_reset(group_id, revision):
                raise ChangedConnection("Подключение уже изменилось. Открой группу заново.")
            for key in list(self.cache):
                if key[0] == group_id:
                    del self.cache[key]
