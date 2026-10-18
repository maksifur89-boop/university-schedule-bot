from html import escape

from aiohttp import web

from group_service import ChangedConnection, GroupService
from storage import StorageError
from university import ScheduleError


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    # Обычный POST формы при no-referrer отправляет Origin: null.
    # same-origin сохраняет проверку Origin и не передаёт ссылку другим сайтам.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def page(title: str, content: str, status: int = 200) -> web.Response:
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{font:17px/1.5 system-ui,sans-serif;background:#f3f5f8;color:#17253c;margin:0;padding:24px}}
main{{max-width:440px;margin:28px auto;background:white;padding:28px;border-radius:20px}}
h1{{font-size:25px;line-height:1.25}}p{{color:#465267}}
label{{display:block;margin:18px 0 8px}}input:not([type=checkbox]){{box-sizing:border-box;width:100%;
padding:13px;border:1px solid #b9c4d2;border-radius:9px;font:inherit}}
button{{width:100%;padding:14px;border:0;border-radius:9px;background:#1766d5;color:white;
font:inherit;font-weight:600;margin-top:22px;cursor:pointer}}
.note{{font-size:14px}}.error{{padding:12px;background:#fff0ed;border-radius:9px;color:#842e20}}
</style></head><body><main>{content}</main></body></html>"""
    return web.Response(text=document, content_type="text/html", status=status,
                        headers=SECURITY_HEADERS)


def form(group: str, token: str, error: str = "") -> web.Response:
    error_block = f'<p class="error">{escape(error)}</p>' if error else ""
    return page("Подключение расписания", f"""
<h1>Расписание группы {escape(group)}</h1>
<p>Подключи свой кабинет вуза, чтобы участники этой группы могли получать расписание.</p>
{error_block}
<form action="/connect" method="post">
<input type="hidden" name="token" value="{escape(token, quote=True)}">
<label for="username">Логин от кабинета вуза</label>
<input id="username" name="username" autocomplete="username" maxlength="200" required>
<label for="password">Пароль от кабинета вуза</label>
<input id="password" name="password" type="password" autocomplete="current-password"
maxlength="500" required>
<label class="note"><input type="checkbox" name="consent" value="yes" required>
Разрешаю боту сохранять данные входа в зашифрованном виде и использовать мой кабинет
для загрузки расписания этой группы. Одногруппники получат только расписание.</label>
<button type="submit">Подключить группу</button>
</form><p class="note">Ссылка действует 15 минут. Пароль от Telegram или Google здесь не нужен.
Данные входа обрабатывает сервер бота в Render, зашифрованная копия хранится в Neon.
Чтобы удалить своё подключение, используй /disconnect в боте.</p>""")


def register_account_pages(app: web.Application, service: GroupService) -> None:
    async def show(request: web.Request) -> web.Response:
        token = request.query.get("token", "")
        try:
            ticket = await service.store.get_ticket(token)
            if ticket is None:
                return page("Ссылка устарела", "<h1>Ссылка устарела</h1><p>Вернись в бот, "
                            "нажми «Моя группа» и открой новую ссылку подключения.</p>", 410)
            return form(ticket["number"], token)
        except StorageError as error:
            return page("Попробуй позже", f"<h1>Попробуй позже</h1><p>{escape(str(error))}</p>", 503)

    async def submit(request: web.Request) -> web.Response:
        origin = request.headers.get("Origin")
        if origin and origin != service.base_url:
            return page("Ошибка", "<h1>Открой форму через бота</h1>", 403)
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length)
        if request.content_type != "application/x-www-form-urlencoded":
            return page("Ошибка", "<h1>Используй форму подключения</h1>", 415)
        data = await request.post()
        token = data.get("token", "")
        if not isinstance(token, str):
            return page("Ошибка", "<h1>Неверная ссылка</h1>", 400)
        try:
            ticket = await service.store.claim_attempt(token)
            if ticket is None:
                return page("Ссылка устарела", "<h1>Подключение уже изменилось</h1>"
                            "<p>Вернись в бот и нажми «Моя группа».</p>", 410)
            username, password = data.get("username", ""), data.get("password", "")
            if (not isinstance(username, str) or not isinstance(password, str)
                    or not 1 <= len(username.strip()) <= 200 or not 1 <= len(password) <= 500
                    or data.get("consent") != "yes"):
                return form(ticket["number"], token, "Заполни логин, пароль и согласие на подключение.")
            try:
                await service.attach(ticket, username.strip(), password)
            except ScheduleError as error:
                return form(ticket["number"], token, str(error))
            return page("Группа подключена", "<h1>✅ Группа подключена</h1><p>Вернись в Telegram "
                        "и нажми «На сегодня», «На завтра» или «На неделю».</p>")
        except ChangedConnection:
            return page("Группа уже подключена", "<h1>Подключение уже изменилось</h1>"
                        "<p>Вернись в бот и нажми «Моя группа».</p>", 409)
        except StorageError as error:
            return page("Попробуй позже", f"<h1>Попробуй позже</h1><p>{escape(str(error))}</p>", 503)

    app.router.add_get("/connect", show)
    app.router.add_post("/connect", submit)
