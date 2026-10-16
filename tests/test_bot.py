"""Run real PTB dispatch with a fake HTTP transport; never contact Telegram."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, ExtBot
from telegram.request import BaseRequest

from database import Database, MAX_TITLE_LENGTH
from main import MENU, build_application
from task_handlers import STATE_KEY


class OfflineRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self.next_error = None

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((api_method, params))
        if self.next_error and api_method == "editMessageText":
            error, self.next_error = self.next_error, None
            raise error
        if api_method == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        elif api_method in ("sendMessage", "editMessageText"):
            result = {
                "message_id": len(self.calls), "date": 0,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "from": {"id": 999, "is_bot": True, "first_name": "Test"},
                "text": params["text"],
            }
        elif api_method == "answerCallbackQuery":
            result = True
        else:
            raise AssertionError(f"Unexpected API method: {api_method}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "test.db")
        self.request = OfflineRequest()
        bot = ExtBot("123:offline-test", request=self.request, get_updates_request=OfflineRequest())
        builder = Application.builder().bot(bot)
        # The preconfigured bot owns a fake token and fake transports.
        with (
            patch("main.Application.builder", return_value=builder),
            patch("telegram.ext.ApplicationBuilder.token", return_value=builder),
        ):
            self.app = build_application("unused", self.db)
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.update_id = 0

    async def send(self, text, user=101, chat_type="private"):
        self.update_id += 1
        message = {
            "message_id": self.update_id, "date": 0,
            "chat": {"id": user if chat_type == "private" else -500, "type": chat_type},
            "from": {"id": user, "first_name": f"User{user}", "is_bot": False}, "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        update = Update.de_json({"update_id": self.update_id, "message": message}, self.app.bot)
        await self.app.process_update(update)

    async def click(self, data, user=101):
        self.update_id += 1
        update = Update.de_json({
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "chat_instance": "offline", "data": data,
                "from": {"id": user, "first_name": f"User{user}", "is_bot": False},
                "message": {
                    "message_id": 77, "date": 0, "chat": {"id": user, "type": "private"},
                    "from": {"id": 999, "is_bot": True, "first_name": "Test"}, "text": "Меню",
                },
            },
        }, self.app.bot)
        await self.app.process_update(update)

    def last_message(self):
        return next(params for method, params in reversed(self.request.calls)
                    if method in ("sendMessage", "editMessageText"))

    def buttons(self):
        markup = self.last_message()["reply_markup"]
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [b for row in markup["inline_keyboard"] for b in row]

    def confirm_button(self):
        return next(b["callback_data"] for b in self.buttons() if b["callback_data"].startswith("tasks:confirm:"))

    async def test_start_help_and_user_registration(self):
        await self.send("/start")
        self.assertIn("Привет", self.last_message()["text"])
        markup = self.last_message()["reply_markup"]
        if isinstance(markup, str):
            markup = json.loads(markup)
        self.assertEqual(markup, MENU.to_dict())
        await self.send("/help")
        self.assertIn("/delete", self.last_message()["text"])
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    async def test_add_direct_works_without_start(self):
        await self.send("/add Купить  хлеб\nи чай")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Купить  хлеб\nи чай")

    async def test_add_by_menu_and_followup_text(self):
        await self.send("📋 Задачи")
        self.assertIn("tasks:add", [b["callback_data"] for b in self.buttons()])
        await self.click("tasks:add")
        await self.send("Позвонить маме")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Позвонить маме")
        self.assertNotIn(STATE_KEY, self.app.user_data[101])

    async def test_add_command_prompts_and_invalid_title_can_be_retried(self):
        await self.send("/add")
        await self.send("а" * (MAX_TITLE_LENGTH + 1))
        self.assertEqual(self.db.list_tasks(101).total, 0)
        self.assertEqual(self.app.user_data[101][STATE_KEY]["action"], "add")
        await self.send(" \n\t")
        self.assertEqual(self.db.list_tasks(101).total, 0)
        await self.send("Нормальная задача")
        self.assertEqual(self.db.list_tasks(101).total, 1)

    async def test_cancel_stops_pending_creation(self):
        await self.send("/add")
        await self.send("/cancel")
        await self.send("Не сохранять")
        self.assertEqual(self.db.list_tasks(101).total, 0)

    async def test_switching_main_menu_does_not_save_button_as_title(self):
        for text in ("/start", "/help", "📋 Задачи", "ℹ️ Помощь", "📝 Заметки"):
            with self.subTest(text=text):
                await self.send("/add")
                await self.send(text)
                self.assertNotIn(STATE_KEY, self.app.user_data[101])
        self.assertEqual(self.db.list_tasks(101).total, 0)

    async def test_tasks_and_navigation(self):
        await self.send("/add First")
        for n in range(6):
            self.db.add_task(101, f"Задача {n}")
        await self.send("/tasks")
        self.assertIn("Страница 1/2", self.last_message()["text"])
        self.assertIn("tasks:list:1", [b["callback_data"] for b in self.buttons()])
        await self.click("tasks:list:1")
        self.assertIn("Страница 2/2", self.last_message()["text"])
        self.assertIn("First", self.last_message()["text"])

    async def test_done_by_command_and_button(self):
        await self.send("/add One")
        one = self.db.list_tasks(101).tasks[0].id
        two = self.db.add_task(101, "Two")
        await self.send(f"/done {one}")
        self.assertTrue(self.db.get_task(101, one).completed)
        await self.click(f"tasks:done:{two}:0")
        self.assertTrue(self.db.get_task(101, two).completed)
        await self.send(f"/done {one}")
        self.assertIn("уже выполнена", self.last_message()["text"])

    async def test_action_commands_without_id_show_selection(self):
        await self.send("/add One")
        await self.send("/done")
        self.assertIn("для выполнения", self.last_message()["text"])
        await self.send("/delete")
        self.assertIn("для удаления", self.last_message()["text"])

    async def test_delete_requires_confirmation_and_cannot_repeat(self):
        await self.send("/add One")
        task_id = self.db.list_tasks(101).tasks[0].id
        await self.send(f"/delete {task_id}")
        self.assertIsNotNone(self.db.get_task(101, task_id))
        confirmation = self.confirm_button()
        await self.click(confirmation)
        self.assertIsNone(self.db.get_task(101, task_id))
        await self.click(confirmation)
        self.assertIn("устарело", self.last_message()["text"])

    async def test_cancel_invalidates_old_confirmation(self):
        await self.send("/add One")
        task_id = self.db.list_tasks(101).tasks[0].id
        await self.click(f"tasks:delete:{task_id}:0")
        confirmation = self.confirm_button()
        await self.send("/cancel")
        await self.click(confirmation)
        self.assertIsNotNone(self.db.get_task(101, task_id))
        self.assertIn("устарело", self.last_message()["text"])

    async def test_reject_button_preserves_task(self):
        await self.send("/add One")
        task_id = self.db.list_tasks(101).tasks[0].id
        await self.send(f"/delete {task_id}")
        cancel = next(b["callback_data"] for b in self.buttons() if b["callback_data"].startswith("tasks:reject:"))
        await self.click(cancel)
        self.assertIsNotNone(self.db.get_task(101, task_id))
        self.assertIn("отменено", self.last_message()["text"])

    async def test_new_delete_request_invalidates_previous_confirmation(self):
        await self.send("/add One")
        one = self.db.list_tasks(101).tasks[0].id
        two = self.db.add_task(101, "Two")
        await self.send(f"/delete {one}")
        old = self.confirm_button()
        await self.send(f"/delete {two}")
        current = self.confirm_button()
        await self.click(old)
        self.assertIsNotNone(self.db.get_task(101, one))
        await self.click(current)
        self.assertIsNone(self.db.get_task(101, two))

    async def test_other_user_cannot_access_task_or_confirmation(self):
        await self.send("/add Secret")
        task_id = self.db.list_tasks(101).tasks[0].id
        await self.send(f"/delete {task_id}")
        confirmation = self.confirm_button()
        for data in (f"tasks:done:{task_id}:0", f"tasks:delete:{task_id}:0", confirmation, "tasks:list:0"):
            await self.click(data, user=202)
            self.assertNotIn("Secret", self.last_message()["text"])
        await self.send(f"/done {task_id}", user=202)
        await self.send(f"/delete {task_id}", user=202)
        self.assertFalse(self.db.get_task(101, task_id).completed)
        self.assertEqual(self.db.list_tasks(202).total, 0)

    async def test_creation_state_is_isolated_by_user(self):
        await self.send("/add")
        await self.send("Не задача", user=202)
        await self.send("Моя задача")
        self.assertEqual(self.db.list_tasks(101).total, 1)
        self.assertEqual(self.db.list_tasks(202).total, 0)

    async def test_malformed_command_ids_and_callbacks_are_safe(self):
        for command in ("done", "delete"):
            for value in ("0", "-1", "1 extra", "1 OR 1=1", "9223372036854775808", "9" * 100):
                await self.send(f"/{command} {value}")
                self.assertIn("Использование", self.last_message()["text"])
        for data in ("tasks:nonsense", "tasks:list:-1", "tasks:done:9223372036854775808:0", "tasks:confirm:bad"):
            await self.click(data)
            self.assertIn("/tasks", self.last_message()["text"])

    async def test_group_cannot_expose_or_create_personal_tasks(self):
        await self.send("/add Secret")
        for command in ("/tasks", "/add Public", "/done 1", "/delete 1", "📋 Задачи"):
            await self.send(command, chat_type="group")
            self.assertIn("личном чате", self.last_message()["text"])
            self.assertNotIn("Secret", self.last_message()["text"])
        self.assertEqual(self.db.list_tasks(101).total, 1)

    async def test_repeated_menu_click_ignores_not_modified_error(self):
        self.request.next_error = BadRequest("Message is not modified")
        await self.click("tasks:list:0")
        self.assertIsNone(self.request.next_error)
        self.assertEqual(self.request.calls[-1][0], "editMessageText")

    async def test_uneditable_menu_gets_new_message(self):
        self.request.next_error = BadRequest("Message to edit not found")
        await self.click("tasks:list:0")
        self.assertEqual(self.request.calls[-1][0], "sendMessage")
        self.assertIn("Задач пока нет", self.last_message()["text"])

    async def test_sqlite_failure_is_reported_without_details(self):
        with patch.object(self.db, "list_tasks", side_effect=sqlite3.OperationalError("private details")):
            with self.assertLogs("main", level="ERROR") as logs:
                await self.send("/tasks")
        self.assertIn("База данных временно недоступна", self.last_message()["text"])
        self.assertNotIn("private details", " ".join(logs.output))

    async def test_registration_failure_stops_task_handler(self):
        with patch.object(self.db, "upsert_user", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs("main", level="ERROR"):
                await self.send("/add Do not create")
        self.assertEqual(self.db.list_tasks(101).total, 0)
        self.assertIn("База данных временно недоступна", self.last_message()["text"])

    async def test_emoji_page_fits_telegram_message_limit(self):
        await self.send("/start")
        for _ in range(5):
            self.db.add_task(101, "😀" * MAX_TITLE_LENGTH)
        await self.send("/tasks")
        self.assertLessEqual(len(self.last_message()["text"].encode("utf-16-le")) // 2, 4096)
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in self.buttons()))

    async def test_unknown_command_does_not_become_task_title(self):
        await self.send("/add")
        await self.send("/unknown")
        self.assertIn("Неизвестная команда", self.last_message()["text"])
        self.assertEqual(self.db.list_tasks(101).total, 0)

    async def test_bot_username_in_command(self):
        await self.send("/add@test_bot Название")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Название")
