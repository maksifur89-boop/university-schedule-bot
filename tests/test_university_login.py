import unittest
from datetime import date
from urllib.parse import parse_qs

import httpx

from university import AccountError, BASE_URL, ScheduleError, UniversityClient


DIRECT_FORM = '''<html><title>Вход в систему</title>
<form name="login_form" method="post" action="/Education/LoginCheck">
<input name="csrf" type="hidden" value="fixture-csrf">
<input name="j_username" type="text">
<input name="j_password" type="password">
<input name="keyboard" type="button" value="Показать клавиатуру">
</form></html>'''
SSO_FORM = '''<form method="post" action="/module.php/core/loginuserpass.php">
<input name="AuthState" type="hidden" value="fixture-state">
<input name="username"><input name="password" type="password"></form>'''
SCHEDULE_PAGE = '''<select id="yearSelector"></select>
<button class="gr-btn" data-groupid="39495">1241</button>'''
LESSON_PAGE = '''<div class="daily-box">
<div class="box-content-header">26.10.2026</div>
<div class="sub-time">13:00 - 14:30</div>
<div class="disc-inf-header">Тестовый предмет / Практика</div>
<div class="box-content-subheader">101</div>
<div class="disc-inf-bottom">Преподаватель - Тестовый преподаватель</div></div>'''


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = UniversityClient("fixture-student", "fixture-password")
        await self.client.client.aclose()
        self.requests = []
        self.handler = None

        def handle(request):
            self.requests.append(request)
            return self.handler(request)

        self.client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_direct_login_preserves_session_fields_and_loads_schedule(self):
        def handle(request):
            if request.url.path.endswith("/LoginCheck"):
                fields = parse_qs(request.content.decode())
                self.assertEqual(fields, {"j_username": ["fixture-student"],
                                         "j_password": ["fixture-password"],
                                         "csrf": ["fixture-csrf"]})
                self.assertEqual(request.headers.get("cookie"), "JSESSIONID=fixture-session")
                return httpx.Response(302, headers={"location": BASE_URL + "Schedule"})
            if request.url.path.endswith("/GenerateScheduleLessons"):
                return httpx.Response(200, text=LESSON_PAGE)
            if any(item.method == "POST" for item in self.requests):
                return httpx.Response(200, text=SCHEDULE_PAGE)
            return httpx.Response(200, text=DIRECT_FORM,
                                  headers={"set-cookie": "JSESSIONID=fixture-session; Path=/Education; Secure"})

        self.handler = handle
        result = await self.client.get_period(date(2026, 10, 26), date(2026, 10, 26))
        self.assertEqual(result[date(2026, 10, 26)][0].starts_at, "13:00")
        self.assertEqual(self.client.groups, {"1241": "39495"})

    async def test_existing_sso_flow_still_works(self):
        def handle(request):
            if request.url.host == "idis.ieml.ru":
                if any(item.method == "POST" and item.url.host == "idis.ieml.ru"
                       for item in self.requests):
                    return httpx.Response(200, text=SCHEDULE_PAGE)
                return httpx.Response(302, headers={"location": "https://idp.ieml.ru/login"})
            if request.method == "GET":
                return httpx.Response(200, text=SSO_FORM)
            fields = parse_qs(request.content.decode())
            self.assertEqual(fields["username"], ["fixture-student"])
            self.assertEqual(fields["password"], ["fixture-password"])
            self.assertEqual(fields["AuthState"], ["fixture-state"])
            return httpx.Response(200, text='''<form method="post"
                action="https://idis.ieml.ru/Education/saml/acs">
                <input name="SAMLResponse" value="fixture-saml"></form>''')

        self.handler = handle
        await self.client.login()
        self.assertTrue(self.client.authenticated)
        self.assertEqual(len([item for item in self.requests if item.method == "POST"]), 2)

    async def test_rejected_direct_credentials_are_submitted_only_once(self):
        self.handler = lambda request: httpx.Response(200, text=DIRECT_FORM)
        with self.assertRaises(AccountError):
            await self.client.login()
        self.assertEqual(len([item for item in self.requests if item.method == "POST"]), 1)

    async def test_unexpected_form_does_not_receive_credentials(self):
        for action in ("https://attacker.example/LoginCheck", "/Education/other",
                       "http://idis.ieml.ru/Education/LoginCheck",
                       "https://idis.ieml.ru:8443/Education/LoginCheck",
                       "https://user@idis.ieml.ru/Education/LoginCheck"):
            with self.subTest(action=action):
                self.requests.clear()
                form = DIRECT_FORM.replace('/Education/LoginCheck', action)
                self.handler = lambda request: httpx.Response(200, text=form)
                with self.assertRaises(ScheduleError):
                    await self.client.login()
                self.assertFalse(any(item.method == "POST" for item in self.requests))

    async def test_unknown_field_names_are_not_treated_as_bad_password(self):
        form = DIRECT_FORM.replace('j_username', 'unexpected_username')
        self.handler = lambda request: httpx.Response(200, text=form)
        with self.assertRaises(ScheduleError) as error:
            await self.client.login()
        self.assertNotIsInstance(error.exception, AccountError)
        self.assertFalse(any(item.method == "POST" for item in self.requests))

    async def test_get_form_is_not_submitted(self):
        self.handler = lambda request: httpx.Response(200, text=DIRECT_FORM.replace('method="post"', 'method="get"'))
        with self.assertRaises(ScheduleError):
            await self.client.login()
        self.assertFalse(any(item.method == "POST" for item in self.requests))

    async def test_expired_direct_session_reauthenticates(self):
        schedule_attempts = 0
        logged_in = False

        def handle(request):
            nonlocal schedule_attempts, logged_in
            if request.url.path.endswith("/GenerateScheduleLessons"):
                schedule_attempts += 1
                return httpx.Response(200, text=DIRECT_FORM if schedule_attempts == 1 else LESSON_PAGE)
            if request.url.path.endswith("/LoginCheck"):
                logged_in = True
                return httpx.Response(302, headers={"location": BASE_URL + "Schedule"})
            return httpx.Response(200, text=SCHEDULE_PAGE if logged_in else DIRECT_FORM)

        self.handler = handle
        self.client.authenticated = True
        self.client.groups = {"1241": "39495"}
        result = await self.client.get_period(date(2026, 10, 26), date(2026, 10, 26))
        self.assertTrue(result[date(2026, 10, 26)])
        self.assertEqual(schedule_attempts, 2)
        self.assertEqual(len([item for item in self.requests if item.method == "POST"]), 1)


if __name__ == "__main__":
    unittest.main()
