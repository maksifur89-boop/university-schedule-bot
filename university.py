import asyncio
import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


BASE_URL = "https://idis.ieml.ru/Education/protected/student-cabinet/"
GROUP_ID = "39495"
ALLOWED_HOSTS = {"idis.ieml.ru", "idp.ieml.ru"}


class ScheduleError(Exception):
    """Не удалось получить достоверное расписание."""


class AccountError(ScheduleError):
    """Сайт подтвердил, что аккаунт больше не даёт доступ к группе."""


def normalize_group(value: str) -> str:
    value = " ".join(value.split()).upper()
    if not re.fullmatch(r"[\w][\w ./()-]{0,39}", value, re.UNICODE):
        raise ValueError("Неверный номер группы.")
    return value


@dataclass(frozen=True)
class Lesson:
    starts_at: str
    ends_at: str
    subject: str
    lesson_type: str
    location: str
    teacher: str


def check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ScheduleError("Сайт перенаправил запрос на неизвестный адрес.")


class UniversityClient:
    def __init__(self, username: str, password: str, group_id: str = GROUP_ID) -> None:
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.authenticated = False
        self.group_id = group_id
        self.groups: dict[str, str] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self, url: str, method: str = "GET", data: dict | None = None
    ) -> httpx.Response:
        for _ in range(20):
            check_url(url)
            response = await self.client.request(method, url, data=data)
            if response.is_redirect:
                url = urljoin(str(response.url), response.headers["location"])
                if response.status_code in (301, 302, 303):
                    method, data = "GET", None
                continue
            response.raise_for_status()
            if 'document.cookie="RCPC="' not in response.text:
                return response
            values = [
                re.search(r'\b' + name + r'=toNumbers\("([0-9a-f]+)"\)', response.text)
                for name in ("a", "b", "c")
            ]
            target = re.search(r'document\.location\.href="([^"]+)"', response.text)
            if not all(values) or not target or "slowAES.decrypt(c,2,a,b)" not in response.text:
                raise ScheduleError("Изменилась проверка браузера на сайте вуза.")
            key, iv, encrypted = [bytes.fromhex(value.group(1)) for value in values]
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            cookie = (decryptor.update(encrypted) + decryptor.finalize()).hex()
            self.client.cookies.set("RCPC", cookie, domain=response.url.host, path="/")
            url = html.unescape(target.group(1))
            method, data = "GET", None
        raise ScheduleError("Слишком много перенаправлений при входе.")

    async def submit_form(self, response: httpx.Response, form) -> httpx.Response:
        fields = {
            element["name"]: element.get("value", "")
            for element in form.select("input[name]")
        }
        if form.select_one('input[type="password"]'):
            fields.update(username=self.username, password=self.password)
        if form.get("method", "get").lower() != "post":
            raise ScheduleError("На сайте изменилась форма входа.")
        target = urljoin(str(response.url), form.get("action", ""))
        if form.select_one('input[type="password"]') and urlparse(target).hostname != "idp.ieml.ru":
            raise ScheduleError("Неожиданный адрес отправки формы входа.")
        return await self.request(target, "POST", fields)

    async def login(self) -> None:
        if not self.username or not self.password:
            raise ScheduleError("В .env отсутствуют данные входа в кабинет вуза.")
        response = await self.request(BASE_URL + "Schedule")
        password_submitted = False
        for _ in range(8):
            soup = BeautifulSoup(response.text, "html.parser")
            if response.url.host == "idis.ieml.ru" and soup.select_one("#yearSelector"):
                self.groups = {}
                for button in soup.select(".gr-btn[data-groupid]"):
                    identifier = button.get("data-groupid", "")
                    if identifier.isdecimal():
                        self.groups[normalize_group(button.get_text(" ", strip=True))] = identifier
                if not self.groups:
                    raise ScheduleError("Не удалось определить группы в кабинете вуза.")
                self.authenticated = True
                return
            form = next(
                (
                    form for form in soup.find_all("form")
                    if form.select_one('input[type="password"], input[name="SAMLResponse"], input[name="SAMLRequest"]')
                ),
                None,
            )
            if form is None:
                if (
                    response.url.host == "idis.ieml.ru"
                    and response.url.path.startswith("/Education/protected/")
                    and response.url.path != urlparse(BASE_URL + "Schedule").path
                ):
                    response = await self.request(BASE_URL + "Schedule")
                    continue
                raise ScheduleError("Не удалось войти в кабинет вуза.")
            if form.select_one('input[type="password"]'):
                if response.url.host != "idp.ieml.ru":
                    raise ScheduleError("Неожиданный адрес формы входа.")
                if password_submitted:
                    raise AccountError("Сайт отклонил логин или пароль от кабинета вуза.")
                password_submitted = True
            response = await self.submit_form(response, form)
        raise ScheduleError("Вход в кабинет не завершился. Проверь логин и пароль.")

    async def available_groups(self) -> dict[str, str]:
        async with self.lock:
            try:
                await self.login()
                return dict(self.groups)
            except (httpx.HTTPError, ValueError):
                raise ScheduleError("Не удалось загрузить группы с сайта вуза.") from None

    async def get_day(self, day: date) -> list[Lesson]:
        schedule = await self.get_period(day, day)
        return schedule[day]

    async def get_period(self, start: date, end: date) -> dict[date, list[Lesson]]:
        if end < start:
            raise ScheduleError("Дата окончания расписания раньше даты начала.")
        async with self.lock:
            try:
                if not self.authenticated:
                    await self.login()
                if self.group_id not in self.groups.values():
                    raise AccountError("Этот аккаунт больше не связан с выбранной группой.")
                parameters = {
                    "par_begindate": start.strftime("%d.%m.%Y"),
                    "par_enddate": end.strftime("%d.%m.%Y"),
                    "par_groupid": self.group_id,
                }
                url = str(httpx.URL(BASE_URL + "GenerateScheduleLessons", params=parameters))
                response = await self.request(url)
                if response.url.host != "idis.ieml.ru":
                    self.authenticated = False
                    await self.login()
                    response = await self.request(url)
                if response.url.path != urlparse(url).path:
                    raise ScheduleError("Сайт вернул страницу входа вместо расписания.")
                return parse_schedule(response.text, start, end)
            except (httpx.HTTPError, ValueError):
                self.authenticated = False
                raise ScheduleError("Ошибка соединения с сайтом вуза.") from None


def parse_lessons(markup: str, day: date) -> list[Lesson]:
    return parse_schedule(markup, day, day)[day]


def parse_schedule(markup: str, start: date, end: date) -> dict[date, list[Lesson]]:
    if end < start:
        raise ScheduleError("Дата окончания расписания раньше даты начала.")
    schedule = {
        start + timedelta(days=offset): []
        for offset in range((end - start).days + 1)
    }
    soup = BeautifulSoup(markup, "html.parser")
    cards = soup.select(".daily-box")
    if not cards:
        text = soup.get_text(" ", strip=True).casefold()
        if not text or any(
            phrase in text for phrase in ("занятий нет", "нет занятий", "занятия не запланированы")
        ):
            return schedule
        raise ScheduleError("Сайт вернул неожиданный ответ вместо списка занятий.")
    for card in cards:
        time_node = card.select_one(".sub-time")
        date_node = card.select_one(".box-content-header")
        subject_node = card.select_one(".disc-inf-header")
        if time_node is None or date_node is None or subject_node is None:
            raise ScheduleError("На сайте изменился формат занятия.")
        lesson_date = datetime.strptime(date_node.get_text(strip=True), "%d.%m.%Y").date()
        if lesson_date not in schedule:
            raise ScheduleError("Сайт вернул занятие за пределами выбранного периода.")
        times = re.fullmatch(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", time_node.get_text(strip=True))
        if times is None:
            raise ScheduleError("На сайте изменился формат времени пары.")
        subject = " ".join(subject_node.get_text(" ", strip=True).split()).split(" / ", 2)
        location = ", ".join(
            node.get_text(" ", strip=True) for node in card.select(".box-content-subheader")
        )
        teacher_node = card.select_one(".disc-inf-bottom")
        teacher = teacher_node.get_text(" ", strip=True) if teacher_node else ""
        teacher = teacher.split(" - ", 1)[-1]
        schedule[lesson_date].append(Lesson(
            starts_at=times.group(1),
            ends_at=times.group(2),
            subject=subject[0],
            lesson_type=subject[1] if len(subject) > 1 else "Тип занятия не указан",
            location=location or "Место не указано",
            teacher=teacher or "Преподаватель не указан",
        ))
    return {
        day: sorted(lessons, key=lambda lesson: lesson.starts_at)
        for day, lessons in schedule.items()
    }
