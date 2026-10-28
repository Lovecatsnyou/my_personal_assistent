"""News dialogs through real PTB dispatch with fake Telegram and RSS transports."""

import asyncio
import re
import unittest
import warnings

import httpx
from telegram.warnings import PTBUserWarning

from note_handlers import STATE_KEY as NOTE_STATE_KEY
from task_handlers import STATE_KEY as TASK_STATE_KEY
from tests import test_bot
from tests.test_news_service import FEEDS, GAMES, NOW, SCIENCE, SOURCES, TECH, FakeFeeds, fake_resolve, item, rss
from tests.test_weather_service import Clock, FakeOpenMeteo
import news_handlers as nh
import news_service as ns
import news_store as store
import weather_handlers as wh
import weather_service as ws


class NewsBotTests(unittest.IsolatedAsyncioTestCase):
    click_raw = test_bot.BotTests.click
    send_raw = test_bot.BotTests.send
    last_message = test_bot.BotTests.last_message
    buttons = test_bot.BotTests.buttons

    async def asyncSetUp(self):
        await test_bot.BotTests.asyncSetUp(self)
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.filterwarnings("ignore", message="Tasks created via", category=PTBUserWarning)
        self.fake = FakeFeeds()
        self.clock = Clock(NOW)
        self.service = ns.NewsService(SOURCES, transport=self.fake.transport, clock=self.clock, retry_delays=(0,),
                                      resolve=fake_resolve)
        self.addAsyncCleanup(self.service.aclose)
        self.app.bot_data[nh.SERVICE_KEY] = self.service
        self.weather = FakeOpenMeteo()
        weather = ws.WeatherService(transport=self.weather.transport, clock=Clock(), retry_delays=(0, 0))
        self.addAsyncCleanup(weather.aclose)
        self.app.bot_data[wh.SERVICE_KEY] = weather
        for user in (101, 202):
            self.db.upsert_user(user, f"User{user}")

    async def drain(self):
        while tasks := set(self.app.bot_data.get(nh.TASKS_KEY, ())) | set(self.app.bot_data.get(wh.TASKS_KEY, ())):
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send(self, text, user=101, chat_type="private"):
        await self.send_raw(text, user, chat_type)
        await self.drain()

    async def click(self, data, user=101):
        await self.click_raw(data, user)
        await self.drain()

    def callbacks(self):
        return [b["callback_data"] for b in self.buttons()]

    def callback(self, prefix):
        return next(data for data in self.callbacks() if data.startswith(prefix))

    def text(self):
        return self.last_message()["text"]

    def state(self, user=101):
        return self.app.user_data[user].get(nh.STATE_KEY)

    def toast(self):
        return next(params for method, params in reversed(self.request.calls) if method == "answerCallbackQuery")

    def requested(self):
        return sorted(set(self.fake.urls()))

    async def test_menu_button_opens_news_menu(self):
        await self.send("📰 Новости")
        self.assertIn("Новости из RSS-лент: выбери действие", self.text())
        self.assertIn("Интересы не выбраны", self.text())
        self.assertEqual(self.callbacks(), [
            "news:feed", "news:int", "news:cats", "news:find", "news:bms:0", "news:feed:r", "home"])
        self.assertEqual(self.fake.requests, [])

    async def test_news_without_interests_offers_setup(self):
        await self.send("/news")
        self.assertIn("Интересы ещё не выбраны", self.text())
        self.assertEqual(self.callbacks(), ["news:int", "news:all", "news:menu"])
        await self.click("news:feed:r")
        self.assertIn("Интересы ещё не выбраны", self.text())
        self.assertEqual(self.fake.requests, [])

    async def test_interests_and_personal_feed(self):
        await self.send("/interests")
        self.assertIn("Выбрано: ничего", self.text())
        self.assertEqual(sum(b["text"].startswith("⬜") for b in self.buttons()), 6)
        await self.click("news:int:games")
        await self.click("news:int:ai_tech")
        await self.click("news:int:science")
        await self.click("news:int:science")
        self.assertIn("Выбрано: Технологии и ИИ, Игры.", self.text())
        self.assertIn("✅ 🎮 Игры", [b["text"] for b in self.buttons()])
        self.assertEqual(store.get_interests(self.db, 101), ["ai_tech", "games"])
        self.assertEqual(store.get_interests(self.db, 202), [])
        await self.click("news:feed")
        text = self.text()
        self.assertTrue(text.startswith("📰 Моя лента: Технологии и ИИ, Игры\nМатериалов: 4. Страница 1/1"), text)
        self.assertIn("1. Вышла новая игра\n📰 Игровой канал · 🕒 17.10.2026 09:30\n🔗 https://example.org/games/1",
                      text)
        self.assertIn("Компания показала чип", text)
        self.assertIn("4. Игра без даты\nОписание\n📰 Игровой канал · 🕒 дата не указана", text)
        self.assertIn("Время публикаций: UTC", text)
        self.assertEqual(self.requested(), sorted([TECH.url, GAMES.url]))
        params = self.last_message()
        self.assertEqual(self.request.calls[-1][0], "editMessageText")
        self.assertNotIn("parse_mode", params)
        self.assertIn('"is_disabled": true', str(params["link_preview_options"]).replace("True", "true").replace(
            "'", '"'))
        await self.send("📰 Новости")
        self.assertIn("Мои интересы: 🤖 Технологии и ИИ, 🎮 Игры", self.text())

    async def test_pagination(self):
        self.fake.routes[TECH.url] = rss(*(item(f"Новость {n}", f"https://example.com/{n}",
                                                f"Sat, 17 Oct 2026 {20 - n:02d}:00:00 +0000")
                                           for n in range(12)))
        await self.send("/news технологии")
        self.assertIn("📰 🤖 Технологии и ИИ\nМатериалов: 12. Страница 1/3", self.text())
        self.assertEqual([b["text"] for b in self.buttons()][:6], ["🔖 1", "🔖 2", "🔖 3", "🔖 4", "🔖 5", "➡️"])
        requests = len(self.fake.requests)
        await self.click(self.callback("news:pg:"))
        self.assertIn("Страница 2/3", self.text())
        self.assertIn("6. Новость 5", self.text())
        self.assertNotIn("Новость 4\n", self.text())
        await self.click([d for d in self.callbacks() if d.startswith("news:pg:")][-1])
        self.assertIn("Страница 3/3", self.text())
        self.assertIn("12. Новость 11", self.text())
        self.assertEqual([b["text"] for b in self.buttons()][:3], ["🔖 11", "🔖 12", "⬅️"])
        self.assertEqual(len(self.fake.requests), requests)  # pages come from the saved list

    async def test_bookmarks(self):
        await self.send("/news игры")
        await self.click(self.callback("news:bm:"))
        self.assertEqual(self.toast()["text"], "🔖 Сохранено в закладки")
        self.assertIn("1. 🔖 Вышла новая игра", self.text())
        self.assertEqual(self.buttons()[0]["text"], "✅ 1")
        await self.click(self.callback("news:bm:"))
        self.assertEqual(self.toast()["text"], "Эта статья уже в закладках")
        # The article leaves the feed; the bookmark stays.
        self.fake.routes[GAMES.url] = rss()
        self.clock.now += ns.CACHE_TTL + 1
        await self.send("/news игры")
        self.assertIn("Свежих материалов пока нет", self.text())
        await self.send("/bookmarks")
        text = self.text()
        self.assertIn("🔖 Сохранённые статьи — 1. Страница 1/1", text)
        self.assertIn("1. Вышла новая игра\n📰 Игровой канал · 🕒 17.10.2026 09:30\n🔗 https://example.org/games/1",
                      text)
        await self.send("/bookmarks", user=202)
        self.assertIn("Сохранённых статей пока нет", self.text())
        await self.send("/bookmarks")
        delete = self.callback("news:bdel:")
        await self.click(delete, user=202)
        self.assertIn("Сохранённых статей пока нет", self.text())
        self.assertEqual(store.list_bookmarks(self.db, 101).total, 1)
        await self.click(delete)
        self.assertIn("Удалено из закладок", self.text())
        await self.click(delete)
        self.assertIn("Закладка не найдена", self.text())

    async def test_stale_and_foreign_buttons_are_rejected(self):
        await self.send("/news игры")
        old_page, old_bookmark = self.callback("news:re:"), self.callback("news:bm:")
        await self.send("/news наука")
        for data in (old_page, old_bookmark, "news:pg:deadbeef:0", "news:re:deadbeef", "news:bm:deadbeef:0",
                     "news:cat:sport", "news:int:sport", "news:unknown", "news:bms:x", old_bookmark[:-1] + "9"):
            with self.subTest(data=data):
                answered = sum(method == "answerCallbackQuery" for method, _ in self.request.calls)
                await self.click(data)
                self.assertIn("Кнопка устарела", self.text())
                self.assertEqual(sum(method == "answerCallbackQuery" for method, _ in self.request.calls),
                                 answered + 1)
        self.assertEqual(self.toast()["text"], "Список устарел. Открой новости заново.")
        await self.send("/news игры")
        current = self.callback("news:bm:")
        await self.click(current, user=202)
        self.assertIn("Кнопка устарела", self.text())
        self.assertEqual(store.list_bookmarks(self.db, 202).total, 0)

    async def test_categories(self):
        await self.click("news:cats")
        self.assertEqual(self.callbacks()[:7], [f"news:cat:{key}" for key in (
            "ai_tech", "programming", "games", "economy", "world", "science")] + ["news:all"])
        await self.click("news:cat:science")
        self.assertIn("📰 🔬 Наука", self.text())
        self.assertIn("Учёные нашли воду", self.text())
        self.assertEqual(self.requested(), [SCIENCE.url])
        await self.send("/news спорт")
        self.assertIn("Такой категории нет", self.text())
        self.assertIn("news:cat:games", self.callbacks())
        await self.send("/news все")
        self.assertIn("📰 Все категории\nМатериалов: 5", self.text())
        await self.click("news:cat:economy")
        self.assertIn("Свежих материалов пока нет", self.text())  # no test sources in this category

    async def test_search(self):
        await self.send("/newssearch ИГРА")
        text = self.text()
        self.assertIn("🔎 Поиск «ИГРА»\nМатериалов: 2", text)
        self.assertIn("не по всему интернету", text)
        self.assertIn("news:find", self.callbacks())
        await self.send("/newssearch квазар")
        self.assertIn("ничего не найдено", self.text())
        await self.send("/newssearch")
        self.assertEqual(self.state(), {"action": "search"})
        await self.send("я")
        self.assertIn("от 2 до 100 символов", self.text())
        self.assertEqual(self.state(), {"action": "search"})
        await self.send("елочные")
        self.assertIsNone(self.state())
        self.assertIn("Ёлочные игрушки с ИИ", self.text())
        await self.click("news:find")
        self.assertEqual(self.state(), {"action": "search"})
        await self.click("news:cancel")
        self.assertIsNone(self.state())

    async def test_cancel_search(self):
        await self.send("/newssearch")
        await self.send("/cancel")
        self.assertIsNone(self.state())
        self.assertIn("Действие отменено", self.text())
        self.assertIn("news:find", self.callbacks())
        await self.send("процессор")
        self.assertIn("Выбери раздел в меню", self.text())
        self.assertEqual(self.fake.requests, [])

    async def test_sections_interrupt_news_and_back(self):
        await self.send("/newssearch")
        await self.send("/add")
        self.assertIsNone(self.state())
        await self.send("Купить хлеб")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Купить хлеб")
        await self.click("news:find")
        await self.send("🌤 Погода")
        self.assertIsNone(self.state())
        await self.send("/add")
        await self.send("📰 Новости")
        self.assertNotIn(TASK_STATE_KEY, self.app.user_data[101])
        await self.send("/note")
        await self.click("news:find")
        self.assertNotIn(NOTE_STATE_KEY, self.app.user_data[101])
        await self.send("процессор")
        self.assertIn("Новый процессор представлен", self.text())
        self.assertEqual(self.db.list_notes(101).total, 0)
        self.assertEqual(self.db.list_tasks(101).total, 1)
        await self.send("/setcity")
        await self.send("/newssearch")
        await self.send("Москва")
        self.assertIn("По запросу «Москва» ничего не найдено", self.text())
        self.assertEqual(self.weather.requests, [])

    async def test_cancel_targets_active_section(self):
        await self.send("/newssearch")
        await self.send("/cancel")
        self.assertIn("news:find", self.callbacks())
        await self.send("/add")
        await self.send("/cancel")
        self.assertIn("tasks:add", self.callbacks())

    async def test_network_failures(self):
        for source in SOURCES:
            self.fake.routes[source.url] = httpx.Response(503)
        await self.send("/news все")
        self.assertIn("Не удалось загрузить новости ни из одного источника", self.text())
        self.assertIn("VPN", self.text())
        self.assertEqual(self.buttons()[0]["text"], "🔄 Повторить")
        self.fake.routes[TECH.url] = FEEDS[TECH.url]
        self.clock.now += ns.FAILURE_PAUSE
        await self.click(self.callback("news:re:"))
        text = self.text()
        self.assertIn("Новый процессор представлен", text)
        self.assertIn("⚠️ Недоступны сейчас: Игровой канал (ошибка сервера), Наука сегодня (ошибка сервера).", text)
        await self.send("/add Купить молоко")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Купить молоко")

    async def test_stale_articles_are_marked(self):
        await self.send("/news технологии")
        self.clock.now += ns.CACHE_TTL + 60
        self.fake.routes[TECH.url] = httpx.ConnectError("refused")
        await self.send("/news технологии")
        text = self.text()
        self.assertIn("⚠️ Не удалось обновить: Tech Daily (нет соединения; данные получены 16 мин назад).", text)
        self.assertIn("Новый процессор представлен", text)
        self.clock.now += ns.STALE_LIMIT
        await self.send("/news технологии")
        self.assertIn("Не удалось загрузить новости ни из одного источника", self.text())

    async def test_refresh_is_rate_limited(self):
        await self.send("/news все")
        requests = len(self.fake.requests)
        await self.click(self.callback("news:re:"))
        self.assertIn("Ленты обновлялись меньше 2 минут назад", self.text())
        self.assertEqual(len(self.fake.requests), requests)
        self.clock.now += ns.MIN_REFRESH_INTERVAL
        await self.click(self.callback("news:re:"))
        self.assertNotIn("обновлялись", self.text())
        self.assertEqual(len(self.fake.requests), requests + 3)

    async def test_slow_feed_does_not_block_other_sections(self):
        self.fake.gates[TECH.url] = asyncio.Event()
        await self.send_raw("/news технологии")
        self.assertEqual(self.text(), nh.LOADING)
        await self.send_raw("/add Пока грузятся новости")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Пока грузятся новости")
        await self.send_raw("/reminders")
        self.assertIn("Мои напоминания", self.text())
        self.fake.gates[TECH.url].set()
        await self.drain()
        loaded = [params["text"] for method, params in self.request.calls if method == "editMessageText"]
        self.assertTrue(any("Новый процессор представлен" in text for text in loaded))

    async def test_dates_follow_user_timezone(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/news технологии")
        self.assertIn("📰 Tech Daily · 🕒 17.10.2026 11:00", self.text())
        self.assertIn("Время публикаций: Europe/Moscow.", self.text())
        await self.send("/news технологии", user=202)
        self.assertIn("📰 Tech Daily · 🕒 17.10.2026 08:00", self.text())

    async def test_unexpected_error_reaches_error_handler(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        self.service.collect = broken
        await self.send("/news все")
        self.assertIn("Не удалось обработать действие", self.text())

    async def test_group_chat_is_refused(self):
        for command in ("/news", "/newssearch игра", "/interests", "/bookmarks"):
            await self.send(command, chat_type="group")
            self.assertIn("только в личном чате", self.text())
        self.assertEqual(self.fake.requests, [])

    async def test_help_home_and_old_modules(self):
        await self.send("/help")
        for command in ("/news", "/newssearch", "/interests", "/bookmarks", "/weather", "/remind", "/notes", "/add"):
            self.assertIn(command, self.text())
        self.assertNotIn("заготовк", self.text())
        await self.send("📰 Новости")
        await self.click("home")
        self.assertIn("Привет", self.text())
        await self.send("/setcity Москва")
        self.assertIn("Основной город сохранён", self.text())
        await self.send("/timezone Europe/Moscow")
        await self.send("/reminders")
        self.assertIn("Мои напоминания", self.text())
        await self.send("/news все")
        self.assertIn("Материалов: 5", self.text())

    async def test_callback_data_fits_telegram_limit(self):
        datas = []
        for action in (lambda: self.send("📰 Новости"), lambda: self.send("/interests"),
                       lambda: self.click("news:cats"), lambda: self.send("/news все"),
                       lambda: self.click(self.callback("news:bm:")), lambda: self.send("/bookmarks")):
            await action()
            datas += self.callbacks()
        self.assertTrue(all(len(data.encode()) <= 64 for data in datas))
        self.assertTrue(all(re.fullmatch(r"(news:[a-z0-9_:]+|home)", data) for data in datas), datas)


if __name__ == "__main__":
    unittest.main()
