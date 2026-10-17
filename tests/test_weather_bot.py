"""Weather dialogs through real PTB dispatch with fake Telegram and Open-Meteo transports."""

import asyncio
import unittest
import warnings

import httpx
from telegram.warnings import PTBUserWarning

from reminder_store import get_timezone
from task_handlers import STATE_KEY as TASK_STATE_KEY
from note_handlers import STATE_KEY as NOTE_STATE_KEY
from tests import test_bot
from tests.test_weather_service import Clock, FakeOpenMeteo
import weather_handlers as wh
import weather_service as ws
import weather_store as store


class WeatherBotTests(unittest.IsolatedAsyncioTestCase):
    click_raw = test_bot.BotTests.click
    send_raw = test_bot.BotTests.send
    last_message = test_bot.BotTests.last_message
    buttons = test_bot.BotTests.buttons

    async def asyncSetUp(self):
        await test_bot.BotTests.asyncSetUp(self)
        # Background tasks are awaited by drain(); outside run_polling PTB warns it will not.
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.filterwarnings("ignore", message="Tasks created via", category=PTBUserWarning)
        self.fake = FakeOpenMeteo()
        self.clock = Clock()
        service = ws.WeatherService(transport=self.fake.transport, clock=self.clock, retry_delays=(0, 0))
        self.addAsyncCleanup(service.aclose)
        self.app.bot_data[wh.SERVICE_KEY] = service
        for user in (101, 202):
            self.db.upsert_user(user, f"User{user}")

    async def drain(self):
        while tasks := set(self.app.bot_data.get(wh.TASKS_KEY, ())):
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send(self, text, user=101, chat_type="private"):
        await self.send_raw(text, user, chat_type)
        await self.drain()

    async def click(self, data, user=101):
        await self.click_raw(data, user)
        await self.drain()

    def callbacks(self):
        return [b["callback_data"] for b in self.buttons()]

    def text(self):
        return self.last_message()["text"]

    def state(self, user=101):
        return self.app.user_data[user].get(wh.STATE_KEY)

    def saved(self, user=101):
        found = store.get_location(self.db, user)
        return found[0] if found else None

    async def test_menu_button_opens_weather_menu(self):
        await self.send("🌤 Погода")
        self.assertIn("Погода: выбери действие", self.text())
        self.assertIn("Город не выбран", self.text())
        self.assertEqual(self.callbacks(), ["wx:cur:me", "wx:fc:me", "wx:find", "wx:my", "wx:fc:me:r", "home"])

    async def test_setcity_with_single_result_saves_and_shows_weather(self):
        await self.send("/setcity Москва")
        self.assertEqual(self.saved().name, "Москва")
        self.assertEqual(self.saved().timezone, "Europe/Moscow")
        text = self.text()
        self.assertIn("Основной город сохранён: Москва, Россия", text)
        self.assertIn("🌡 Температура: +15 °C", text)
        self.assertIn("Open-Meteo", text)
        # The "⏳" reply was edited into the result.
        self.assertEqual(self.request.calls[-1][0], "editMessageText")
        await self.send("🌤 Погода")
        self.assertIn("Мой город: Москва, Россия", self.text())

    async def test_weather_without_city_asks_for_one(self):
        await self.send("/weather")
        self.assertIn("Основной город ещё не выбран", self.text())
        self.assertEqual(self.state(), {"action": "search", "purpose": "set"})
        await self.send("москва")
        self.assertIsNone(self.state())
        self.assertEqual(self.saved().name, "Москва")
        await self.send("/weather")
        self.assertIn("🌤 Погода — Москва", self.text())

    async def test_same_name_offers_a_choice_and_does_not_change_main_city(self):
        await self.send("/setcity Москва")
        await self.send("/weather Париж")
        text = self.text()
        self.assertIn("Найдено несколько мест", text)
        self.assertIn("1. Париж, Иль-де-Франс, Франция", text)
        self.assertIn("2. Париж, Техас, США", text)
        picks = [data for data in self.callbacks() if data.startswith("wx:pick:")]
        self.assertEqual(len(picks), 2)
        await self.click(picks[1])
        self.assertIn("🌤 Погода — Париж", self.text())
        self.assertIn("Техас, США", self.text())
        self.assertEqual(self.fake.params()["timezone"], "America/Chicago")
        self.assertEqual(self.saved().name, "Москва")
        save = next(data for data in self.callbacks() if data.startswith("wx:save:"))
        await self.click(save)
        self.assertIn("Основной город сохранён: Париж, Техас, США", self.text())
        self.assertEqual(self.saved().region, "Техас")

    async def test_menu_search_and_buttons_for_unsaved_place(self):
        await self.send("🌤 Погода")
        await self.click("wx:find")
        self.assertEqual(self.state(), {"action": "search", "purpose": "cur"})
        await self.send("Berlin")
        self.assertIn("Погода — Берлин", self.text())
        forecast = next(data for data in self.callbacks() if data.startswith("wx:fc:") and data != "wx:fc:me")
        await self.click(forecast)
        self.assertIn("Прогноз на 7 дней — Берлин", self.text())
        self.assertIsNone(self.saved())

    async def test_forecast_for_saved_city_and_argument(self):
        await self.send("/setcity Москва")
        await self.send("/forecast")
        text = self.text()
        self.assertIn("📅 Прогноз на 7 дней — Москва, Россия", text)
        self.assertEqual(text.count("🌡"), 7)
        await self.send("/forecast Berlin")
        self.assertIn("Прогноз на 7 дней — Берлин", self.text())
        self.assertEqual(self.saved().name, "Москва")

    async def test_mycity_and_user_isolation(self):
        await self.send("/mycity")
        self.assertIn("не выбран", self.text())
        await self.send("/setcity Москва")
        await self.send("/mycity")
        for part in ("📍 Москва", "Страна: Россия", "Часовой пояс: Europe/Moscow", "55.7522, 37.6156"):
            self.assertIn(part, self.text())
        await self.send("/mycity", user=202)
        self.assertIn("не выбран", self.text())
        await self.send("/setcity Berlin", user=202)
        self.assertEqual(self.saved(101).name, "Москва")
        self.assertEqual(self.saved(202).name, "Берлин")

    async def test_cache_and_refresh_button(self):
        await self.send("/setcity Москва")
        requests = len(self.fake.requests)
        await self.click("wx:cur:me")
        self.assertEqual(len(self.fake.requests), requests)
        await self.click("wx:cur:me:r")
        self.assertIn("меньше минуты назад", self.text())
        self.assertEqual(len(self.fake.requests), requests)
        self.clock.now += ws.MIN_REFRESH_INTERVAL
        await self.click("wx:cur:me:r")
        self.assertEqual(len(self.fake.requests), requests + 1)
        self.assertNotIn("меньше минуты", self.text())

    async def test_network_failure_with_and_without_cache(self):
        self.fake.failures = [httpx.ConnectTimeout("timed out")] * 3
        await self.send("/setcity Москва")
        self.assertIn("Не удалось выполнить поиск", self.text())
        self.assertIn("сервер не ответил вовремя", self.text())
        self.assertIsNone(self.saved())
        await self.send("/setcity Москва")
        self.clock.now += ws.CURRENT_TTL + 1
        self.fake.failures = [httpx.ConnectError("refused")] * 3
        await self.send("/weather")
        self.assertIn("Показаны последние сохранённые данные", self.text())
        self.assertIn("🔄 Обновить", [b["text"] for b in self.buttons()])
        self.clock.now += ws.CURRENT_STALE_LIMIT
        self.fake.failures = [httpx.Response(503)] * 3
        await self.send("/weather")
        self.assertIn("Сохранённых недавних данных нет", self.text())
        self.assertIn("wx:cur:me:r", self.callbacks())
        # Other sections keep working.
        await self.send("/add Купить хлеб")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Купить хлеб")

    async def test_unexpected_error_reaches_error_handler(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        self.app.bot_data[wh.SERVICE_KEY].search = broken
        await self.send("/weather Москва")
        self.assertIn("Не удалось обработать действие", self.text())

    async def test_invalid_query_keeps_dialog(self):
        await self.send("/setcity")
        await self.send("М")
        self.assertIn("от 2 до 100 символов", self.text())
        self.assertEqual(self.state(), {"action": "search", "purpose": "set"})
        await self.send("а" * 101)
        self.assertEqual(self.state(), {"action": "search", "purpose": "set"})
        self.assertEqual(self.fake.requests, [])

    async def test_stale_and_foreign_buttons_are_rejected(self):
        await self.send("/weather Париж")
        old_pick = next(data for data in self.callbacks() if data.startswith("wx:pick:"))
        await self.send("/weather Париж")
        current_pick = next(data for data in self.callbacks() if data.startswith("wx:pick:"))
        for data in (old_pick, "wx:pick:deadbeef:0", "wx:cur:deadbeef", "wx:save:deadbeef", "wx:unknown",
                     "wx:pick:" + old_pick.split(":")[2] + ":9"):
            with self.subTest(data=data):
                answered = sum(method == "answerCallbackQuery" for method, _ in self.request.calls)
                await self.click(data)
                self.assertTrue("устарел" in self.text() or "Кнопка устарела" in self.text(), self.text())
                self.assertEqual(sum(method == "answerCallbackQuery" for method, _ in self.request.calls),
                                 answered + 1)
        # Another user's results are not valid for this user.
        await self.click(current_pick, user=202)
        self.assertIn("устарели", self.text())
        await self.click(current_pick)
        self.assertIn("Погода — Париж", self.text())

    async def test_cancel_search(self):
        await self.send("/setcity")
        await self.send("/cancel")
        self.assertIsNone(self.state())
        self.assertIn("Действие отменено", self.text())
        self.assertIn("wx:find", self.callbacks())
        await self.send("Москва")
        self.assertIn("Выбери раздел в меню", self.text())
        await self.click("wx:find")
        await self.click("wx:cancel")
        self.assertIsNone(self.state())
        self.assertEqual(self.fake.requests, [])

    async def test_sections_interrupt_weather_and_back(self):
        await self.send("/note")
        await self.send("🌤 Погода")
        self.assertNotIn(NOTE_STATE_KEY, self.app.user_data[101])
        await self.click("wx:find")
        await self.send("/add")
        self.assertIsNone(self.state())
        await self.send("Москва")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Москва")
        self.assertIsNone(self.saved())
        await self.send("/add")
        await self.send("/weather")
        self.assertNotIn(TASK_STATE_KEY, self.app.user_data[101])
        await self.send("Москва")
        self.assertEqual(self.db.list_tasks(101).total, 1)
        self.assertEqual(self.db.list_notes(101).total, 0)
        self.assertEqual(self.saved().name, "Москва")
        await self.send("/setcity")
        await self.click("notes:add")
        self.assertIsNone(self.state())
        await self.send("Только заголовок")
        self.assertEqual(self.app.user_data[101][NOTE_STATE_KEY]["action"], "add_content")

    async def test_cancel_targets_active_section(self):
        await self.send("/setcity")
        await self.send("/cancel")
        self.assertIn("wx:find", self.callbacks())
        await self.send("/note")
        await self.send("/cancel")
        self.assertIn("notes:add", self.callbacks())
        await self.send("/add")
        await self.send("/cancel")
        self.assertIn("tasks:add", self.callbacks())

    async def test_city_zone_does_not_touch_reminder_timezone(self):
        await self.send("/timezone Asia/Yekaterinburg")
        await self.send("/setcity Berlin")
        self.assertEqual(get_timezone(self.db, 101), "Asia/Yekaterinburg")
        await self.send("/timezone UTC")
        self.assertEqual(self.saved().timezone, "Europe/Berlin")

    async def test_home_button_and_help(self):
        await self.send("🌤 Погода")
        await self.click("home")
        self.assertIn("Привет", self.text())
        await self.send("/help")
        for command in ("/weather", "/forecast", "/setcity", "/mycity", "/remind", "/notes", "/add"):
            self.assertIn(command, self.text())

    async def test_group_chat_is_refused(self):
        await self.send("/weather Москва", chat_type="group")
        self.assertIn("только в личном чате", self.text())
        self.assertEqual(self.fake.requests, [])

    async def test_callback_data_fits_telegram_limit(self):
        await self.send("/weather Париж")
        await self.click(next(data for data in self.callbacks() if data.startswith("wx:pick:")))
        datas = self.callbacks()
        await self.send("🌤 Погода")
        datas += self.callbacks()
        self.assertTrue(all(len(data.encode()) <= 64 for data in datas))


if __name__ == "__main__":
    unittest.main()
