"""Note dialogs through real PTB dispatch with the fake transport from test_bot."""

import json
import unittest

from telegram.error import BadRequest

from database import MAX_NOTE_CONTENT_LENGTH, MAX_NOTE_TITLE_LENGTH
from main import MENU
from note_handlers import QUERY_KEY, STATE_KEY, split_message
from task_handlers import STATE_KEY as TASK_STATE_KEY
from tests import test_bot


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


class NoteBotTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the offline application and helpers without re-running the task tests.
    send = test_bot.BotTests.send
    click = test_bot.BotTests.click
    last_message = test_bot.BotTests.last_message
    buttons = test_bot.BotTests.buttons

    async def asyncSetUp(self):
        await test_bot.BotTests.asyncSetUp(self)
        for user in (101, 202):
            self.db.upsert_user(user, f"User{user}")

    def callbacks(self):
        return [b["callback_data"] for b in self.buttons()]

    def callback(self, prefix):
        return next(data for data in self.callbacks() if data.startswith(prefix))

    def state(self, user=101):
        return self.app.user_data[user].get(STATE_KEY)

    def sent_texts(self, since):
        return [params["text"] for method, params in self.request.calls[since:]
                if method in ("sendMessage", "editMessageText")]

    async def create(self, title="Покупки", content="Молоко и хлеб", user=101):
        await self.send("/note", user=user)
        await self.send(title, user=user)
        await self.send(content, user=user)

    async def test_menu_button_opens_note_menu_with_all_actions(self):
        await self.send("📝 Заметки")
        self.assertIn("Заметки", self.last_message()["text"])
        self.assertEqual(self.callbacks(), [
            "notes:add", "notes:list:0", "notes:search", "notes:edit_list:0", "notes:delete_list:0", "home",
        ])

    async def test_home_button_returns_main_keyboard(self):
        await self.send("/note")
        await self.click("home")
        self.assertIsNone(self.state())
        markup = self.last_message()["reply_markup"]
        self.assertEqual(json.loads(markup) if isinstance(markup, str) else markup, MENU.to_dict())

    async def test_create_by_menu_dialog(self):
        await self.send("📝 Заметки")
        await self.click("notes:add")
        self.assertEqual(self.state(), {"action": "add_title"})
        await self.send("Список покупок")
        self.assertEqual(self.state()["action"], "add_content")
        await self.send("Молоко\nХлеб")
        note = self.db.list_notes(101).notes[0]
        self.assertEqual((note.title, note.content), ("Список покупок", "Молоко\nХлеб"))
        self.assertIn("сохранена", self.last_message()["text"])
        self.assertIsNone(self.state())

    async def test_note_command_with_title_asks_for_content(self):
        await self.send("/note Идея")
        self.assertEqual(self.state(), {"action": "add_content", "title": "Идея"})
        await self.send("Текст идеи")
        self.assertEqual(self.db.list_notes(101).notes[0].title, "Идея")

    async def test_invalid_input_keeps_dialog_step(self):
        await self.send("/note")
        await self.send("а" * (MAX_NOTE_TITLE_LENGTH + 1))
        self.assertEqual(self.state(), {"action": "add_title"})
        await self.send("Заголовок")
        await self.send("а" * (MAX_NOTE_CONTENT_LENGTH + 1))
        self.assertEqual(self.state()["action"], "add_content")
        self.assertEqual(self.db.list_notes(101).total, 0)
        await self.send("Нормальный текст")
        self.assertEqual(self.db.list_notes(101).total, 1)

    async def test_cancel_at_every_step(self):
        for steps in ([], ["Заголовок"]):
            with self.subTest(steps=steps):
                await self.send("/note")
                for text in steps:
                    await self.send(text)
                await self.send("/cancel")
                self.assertIsNone(self.state())
                self.assertIn("отменено", self.last_message()["text"])
                await self.send("Не сохранять")
        await self.send("/searchnotes")
        await self.click("notes:cancel")
        self.assertIsNone(self.state())
        self.assertEqual(self.db.list_notes(101).total, 0)

    async def test_list_view_and_pagination(self):
        for n in range(7):
            self.db.add_note(101, f"Заметка {n}", f"Текст {n}")
        await self.send("/notes")
        text = self.last_message()["text"]
        self.assertIn("Страница 1/2", text)
        self.assertIn("Заметка 6", text)
        self.assertIn("🕒", text)
        self.assertIn("notes:list:1", self.callbacks())
        await self.click("notes:list:1")
        self.assertIn("Страница 2/2", self.last_message()["text"])
        self.assertIn("Заметка 0", self.last_message()["text"])
        await self.click(self.callback("notes:open:"))
        card = self.last_message()["text"]
        self.assertIn("Создана:", card)
        self.assertIn("Изменена:", card)
        self.assertIn("notes:list:1", self.callbacks())

    async def test_edit_title_and_content(self):
        await self.create()
        note_id = self.db.list_notes(101).notes[0].id
        await self.send(f"/editnote {note_id}")
        await self.click(f"notes:edit_title:{note_id}:list:0")
        self.assertIn("Покупки", self.last_message()["text"])
        await self.send("Новый заголовок")
        self.assertIn("обновлена", self.last_message()["text"])
        await self.click(f"notes:edit_content:{note_id}:list:0")
        await self.send("Новый текст")
        note = self.db.get_note(101, note_id)
        self.assertEqual((note.title, note.content), ("Новый заголовок", "Новый текст"))
        self.assertIsNone(self.state())

    async def test_edit_list_and_cancel_keeps_note(self):
        await self.create()
        await self.send("/editnote")
        self.assertIn("для редактирования", self.last_message()["text"])
        await self.click(self.callback("notes:open:"))
        await self.click(self.callback("notes:edit_content:"))
        await self.send("/cancel")
        await self.send("Не применять")
        self.assertEqual(self.db.list_notes(101).notes[0].content, "Молоко и хлеб")

    async def test_delete_requires_confirmation_and_cannot_repeat(self):
        await self.create()
        note_id = self.db.list_notes(101).notes[0].id
        await self.send(f"/deletenote {note_id}")
        self.assertIsNotNone(self.db.get_note(101, note_id))
        confirmation = self.callback("notes:confirm:")
        await self.click(confirmation)
        self.assertIsNone(self.db.get_note(101, note_id))
        self.assertIn("удалена", self.last_message()["text"])
        await self.click(confirmation)
        self.assertIn("устарело", self.last_message()["text"])

    async def test_delete_reject_and_cancel_invalidate_confirmation(self):
        await self.create()
        note_id = self.db.list_notes(101).notes[0].id
        await self.click(f"notes:delete:{note_id}:list:0")
        await self.click(self.callback("notes:reject:"))
        self.assertIn("отменено", self.last_message()["text"])
        await self.send("/deletenote")
        await self.click(self.callback("notes:delete:"))
        confirmation = self.callback("notes:confirm:")
        await self.send("/cancel")
        await self.click(confirmation)
        self.assertIsNotNone(self.db.get_note(101, note_id))

    async def test_cyrillic_search_and_result_pages(self):
        self.db.add_note(101, "ПОКУПКИ", "молоко")
        for n in range(6):
            self.db.add_note(101, f"План {n}", "Купить ёлку")
        self.db.add_note(101, "Другое", "ничего")
        await self.send("/searchnotes Купить ЕЛКУ")
        text = self.last_message()["text"]
        self.assertIn("найдено 6", text)
        self.assertNotIn("ПОКУПКИ", text)
        await self.click("notes:found:1")
        self.assertIn("Страница 2/2", self.last_message()["text"])
        await self.click("notes:search")
        await self.send("покупки")
        self.assertIn("найдено 1", self.last_message()["text"])
        self.assertIn("ПОКУПКИ", self.last_message()["text"])

    async def test_search_validation_and_lost_query(self):
        await self.send("/searchnotes")
        await self.send("а" * 201)
        self.assertEqual(self.state(), {"action": "search"})
        await self.send("/cancel")
        self.app.user_data[101].pop(QUERY_KEY, None)
        await self.click("notes:found:0")
        self.assertIn("устарели", self.last_message()["text"])

    async def test_users_are_isolated(self):
        await self.create("Секрет", "Пин-код 1234")
        note_id = self.db.list_notes(101).notes[0].id
        await self.send(f"/deletenote {note_id}")
        confirmation = self.callback("notes:confirm:")
        for data in (f"notes:open:{note_id}:list:0", f"notes:edit_title:{note_id}:list:0",
                     f"notes:delete:{note_id}:list:0", confirmation, "notes:list:0"):
            await self.click(data, user=202)
            self.assertNotIn("Секрет", self.last_message()["text"])
        await self.send("/searchnotes секрет", user=202)
        self.assertIn("найдено 0", self.last_message()["text"])
        await self.send(f"/editnote {note_id}", user=202)
        self.assertNotIn("Пин-код", self.last_message()["text"])
        self.assertEqual(self.db.get_note(101, note_id).title, "Секрет")

    async def test_dialog_state_is_isolated_by_user(self):
        await self.send("/note")
        await self.send("Чужой текст", user=202)
        self.assertEqual(self.state(), {"action": "add_title"})
        self.assertIsNone(self.app.user_data[202].get(STATE_KEY))

    async def test_stale_and_malformed_buttons(self):
        await self.create()
        note_id = self.db.list_notes(101).notes[0].id
        self.db.delete_note(101, note_id)
        for data in (f"notes:open:{note_id}:list:0", f"notes:edit_title:{note_id}:list:0",
                     f"notes:delete:{note_id}:list:0"):
            await self.click(data)
            self.assertIn("не найдена", self.last_message()["text"])
            self.assertIsNone(self.state())
        for data in ("notes:nonsense", "notes:list:-1", "notes:open:9223372036854775808:list:0",
                     "notes:open:1:evil:0", "notes:confirm:bad"):
            await self.click(data)
            self.assertIn("/notes", self.last_message()["text"])

    async def test_edit_of_note_deleted_meanwhile(self):
        await self.create()
        note_id = self.db.list_notes(101).notes[0].id
        await self.click(f"notes:edit_title:{note_id}:list:0")
        self.db.delete_note(101, note_id)
        await self.send("Новое")
        self.assertIn("не найдена", self.last_message()["text"])
        self.assertIsNone(self.state())

    async def test_malformed_command_ids(self):
        for command in ("editnote", "deletenote"):
            for value in ("0", "-1", "1 2", "1 OR 1=1", "9" * 30):
                await self.send(f"/{command} {value}")
                self.assertIn("Использование", self.last_message()["text"])

    async def test_group_chat_cannot_use_notes(self):
        await self.create("Секрет", "Текст")
        for command in ("/notes", "/note Публично", "/searchnotes Секрет", "/editnote 1",
                        "/deletenote 1", "📝 Заметки"):
            await self.send(command, chat_type="group")
            self.assertIn("личном чате", self.last_message()["text"])
            self.assertNotIn("Секрет", self.last_message()["text"])
        self.assertEqual(self.db.list_notes(101).total, 1)

    async def test_long_note_is_split_within_telegram_limit(self):
        note_id = self.db.add_note(101, "😀" * MAX_NOTE_TITLE_LENGTH, "😀" * MAX_NOTE_CONTENT_LENGTH)
        since = len(self.request.calls)
        await self.click(f"notes:open:{note_id}:list:0")
        texts = self.sent_texts(since)
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(utf16_length(text) <= 4096 for text in texts))
        self.assertEqual("".join(texts).count("😀"), MAX_NOTE_TITLE_LENGTH + MAX_NOTE_CONTENT_LENGTH)
        self.assertIn("notes:menu", self.callbacks())

    async def test_list_page_fits_telegram_limit(self):
        for _ in range(5):
            self.db.add_note(101, "😀" * MAX_NOTE_TITLE_LENGTH, "😀" * MAX_NOTE_CONTENT_LENGTH)
        await self.send("/notes")
        self.assertLessEqual(utf16_length(self.last_message()["text"]), 4096)
        await self.send("/searchnotes " + "😀" * 200)
        self.assertLessEqual(utf16_length(self.last_message()["text"]), 4096)
        self.assertTrue(all(len(data.encode()) <= 64 for data in self.callbacks()))

    def test_split_message_counts_utf16(self):
        self.assertEqual(split_message("a" * 5, 2), ["aa", "aa", "a"])
        self.assertEqual(split_message("😀😀a", 3), ["😀", "😀a"])

    async def test_switching_sections_drops_other_dialog(self):
        await self.send("/add")
        await self.send("/note")
        self.assertNotIn(TASK_STATE_KEY, self.app.user_data[101])
        await self.send("Заголовок")
        await self.send("/add")
        self.assertIsNone(self.state())
        await self.send("Задача")
        self.assertEqual(self.db.list_tasks(101).tasks[0].title, "Задача")
        self.assertEqual(self.db.list_notes(101).total, 0)
        await self.send("/note")
        await self.click("tasks:menu")
        self.assertIsNone(self.state())
        await self.send("📝 Заметки")
        await self.click("notes:add")
        await self.send("📋 Задачи")
        self.assertIsNone(self.state())
        self.assertEqual(self.db.list_notes(101).total, 0)

    async def test_cancel_targets_active_section(self):
        await self.send("/note")
        await self.send("/cancel")
        self.assertIn("notes:add", self.callbacks())
        await self.send("/add")
        await self.send("/cancel")
        self.assertIn("tasks:add", self.callbacks())

    async def test_task_confirmation_is_invalidated_by_note_action(self):
        await self.send("/add Задача")
        task_id = self.db.list_tasks(101).tasks[0].id
        await self.send(f"/delete {task_id}")
        confirmation = self.callback("tasks:confirm:")
        await self.send("/notes")
        await self.click(confirmation)
        self.assertIsNotNone(self.db.get_task(101, task_id))

    async def test_uneditable_menu_gets_new_message(self):
        self.request.next_error = BadRequest("Message to edit not found")
        await self.click("notes:list:0")
        self.assertEqual(self.request.calls[-1][0], "sendMessage")
        self.assertIn("Заметок пока нет", self.last_message()["text"])

    async def test_help_lists_note_commands(self):
        await self.send("/help")
        for command in ("/note", "/notes", "/searchnotes", "/editnote", "/deletenote", "/delete"):
            self.assertIn(command, self.last_message()["text"])
