import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import certifi
import psycopg
from psycopg.rows import dict_row

from university import normalize_group


class StorageError(Exception):
    """Ошибка хранилища без раскрытия адресов и паролей."""


@dataclass(frozen=True)
class Group:
    id: int
    number: str
    university_id: str | None
    credentials: str | None = field(repr=False)
    owner_id: int | None
    revision: int
    failure_kind: str | None


def group_from_row(row: dict | None) -> Group | None:
    if row is None:
        return None
    return Group(**{key: row[key] for key in Group.__dataclass_fields__})


def ticket_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def connection(self):
        try:
            async with self._slots, asyncio.timeout(20):
                async with await psycopg.AsyncConnection.connect(
                    self._database_url,
                    autocommit=True,
                    connect_timeout=8,
                    prepare_threshold=None,
                    row_factory=dict_row,
                    sslmode="verify-full",
                    sslrootcert=certifi.where(),
                ) as connection:
                    yield connection
        except (psycopg.Error, TimeoutError):
            raise StorageError("Хранилище временно недоступно. Попробуй позже.") from None

    async def initialize(self) -> None:
        source = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        async with self.connection() as connection:
            async with connection.transaction():
                await connection.execute(source, prepare=False)

    async def get_group(self, group_id: int) -> Group | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM schedule_bot.groups WHERE id = %s", (group_id,)
            )
            return group_from_row(await cursor.fetchone())

    async def user_group(self, telegram_id: int) -> Group | None:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT g.* FROM schedule_bot.groups g JOIN schedule_bot.users u "
                "ON u.group_id = g.id WHERE u.telegram_id = %s", (telegram_id,)
            )
            return group_from_row(await cursor.fetchone())

    async def choose_group(self, telegram_id: int, number: str) -> Group:
        number = normalize_group(number)
        async with self.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    "INSERT INTO schedule_bot.groups (number) VALUES (%s) "
                    "ON CONFLICT (number) DO UPDATE SET number = EXCLUDED.number RETURNING *",
                    (number,),
                )
                group = group_from_row(await cursor.fetchone())
                await connection.execute(
                    "INSERT INTO schedule_bot.users (telegram_id, group_id) VALUES (%s, %s) "
                    "ON CONFLICT (telegram_id) DO UPDATE SET group_id = EXCLUDED.group_id, "
                    "updated_at = now()", (telegram_id, group.id)
                )
                return group

    async def new_ticket(self, telegram_id: int, group: Group) -> str:
        token = secrets.token_urlsafe(32)
        async with self.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM schedule_bot.login_tickets WHERE expires_at < now() "
                    "OR telegram_id = %s", (telegram_id,)
                )
                cursor = await connection.execute(
                    "INSERT INTO schedule_bot.login_tickets "
                    "(token_hash, telegram_id, group_id, revision, expires_at) "
                    "SELECT %s, u.telegram_id, g.id, g.revision, now() + interval '15 minutes' "
                    "FROM schedule_bot.users u JOIN schedule_bot.groups g ON g.id = u.group_id "
                    "WHERE u.telegram_id = %s AND g.id = %s AND g.revision = %s "
                    "AND g.credentials IS NULL RETURNING token_hash",
                    (ticket_hash(token), telegram_id, group.id, group.revision),
                )
                if await cursor.fetchone() is None:
                    raise StorageError("Подключение уже изменилось. Открой «Моя группа» в боте.")
        return token

    async def get_ticket(self, token: str) -> dict | None:
        if not 30 <= len(token) <= 100:
            return None
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT t.telegram_id, t.group_id, t.revision, g.number "
                "FROM schedule_bot.login_tickets t JOIN schedule_bot.groups g ON g.id = t.group_id "
                "JOIN schedule_bot.users u ON u.telegram_id = t.telegram_id "
                "WHERE t.token_hash = %s AND t.expires_at > now() "
                "AND g.revision = t.revision AND g.credentials IS NULL AND u.group_id = t.group_id",
                (ticket_hash(token),),
            )
            return await cursor.fetchone()

    async def claim_attempt(self, token: str) -> dict | None:
        ticket = await self.get_ticket(token)
        if ticket is None:
            return None
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE schedule_bot.users SET "
                "login_attempts = CASE WHEN login_window < now() - interval '1 hour' "
                "THEN 1 ELSE login_attempts + 1 END, "
                "login_window = CASE WHEN login_window < now() - interval '1 hour' "
                "THEN now() ELSE login_window END "
                "WHERE telegram_id = %s AND group_id = %s "
                "AND (login_attempts < 5 OR login_window < now() - interval '1 hour') "
                "RETURNING telegram_id", (ticket["telegram_id"], ticket["group_id"])
            )
            if await cursor.fetchone() is None:
                raise StorageError("Достигнут лимит: 5 попыток входа в час. Попробуй позже.")
        return ticket

    async def attach(self, group: Group, university_id: str, credentials: str,
                     owner_id: int | None) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE schedule_bot.groups SET university_id = %s, credentials = %s, "
                "owner_id = %s, revision = revision + 1, failure_kind = NULL, failed_at = NULL, "
                "last_success_at = now() WHERE id = %s AND revision = %s "
                "AND credentials IS NULL RETURNING id",
                (university_id, credentials, owner_id, group.id, group.revision),
            )
            return await cursor.fetchone() is not None

    async def mark_result(self, group: Group, failed: bool) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE schedule_bot.groups SET failure_kind = %s, "
                "failed_at = CASE WHEN %s THEN now() ELSE NULL END, "
                "last_success_at = CASE WHEN %s THEN last_success_at ELSE now() END "
                "WHERE id = %s AND revision = %s RETURNING id",
                ("account" if failed else None, failed, failed, group.id, group.revision),
            )
            return await cursor.fetchone() is not None

    async def reset(self, telegram_id: int, group: Group) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE schedule_bot.groups g SET credentials = NULL, university_id = NULL, "
                "owner_id = NULL, revision = revision + 1, failure_kind = NULL, failed_at = NULL "
                "WHERE g.id = %s AND g.revision = %s AND g.failure_kind = 'account' "
                "AND EXISTS (SELECT 1 FROM schedule_bot.users u WHERE u.telegram_id = %s "
                "AND u.group_id = g.id) RETURNING id", (group.id, group.revision, telegram_id)
            )
            return await cursor.fetchone() is not None

    async def disconnect_owner(self, telegram_id: int, group: Group) -> bool:
        async with self.connection() as connection:
            cursor = await connection.execute(
                "UPDATE schedule_bot.groups SET credentials = NULL, university_id = NULL, "
                "owner_id = NULL, revision = revision + 1, failure_kind = NULL, failed_at = NULL "
                "WHERE id = %s AND revision = %s AND owner_id = %s RETURNING id",
                (group.id, group.revision, telegram_id),
            )
            return await cursor.fetchone() is not None
