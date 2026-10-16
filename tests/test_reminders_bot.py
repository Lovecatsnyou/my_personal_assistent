"""Reminder dialogs, JobQueue delivery and restore through real PTB dispatch, offline.

The clock is fixed at NOW; jobs are executed directly with Job.run instead of waiting.
"""

from datetime import date, datetime, time, timedelta, timezone
import json
import re
import unittest
from unittest.mock import patch

from telegram.error import Forbidden, NetworkError

from note_handlers import STATE_KEY as NOTE_STATE_KEY
from reminder_handlers import STATE_KEY
from reminder_schedule import Schedule
import reminder_scheduler
import reminder_store as store
from task_handlers import STATE_KEY as TASK_STATE_KEY
from tests import test_bot

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # 15:00 in Moscow


class ReminderBotTests(unittest.IsolatedAsyncioTestCase):
    send = test_bot.BotTests.send
    click = test_bot.BotTests.click
    last_message = test_bot.BotTests.last_message
    buttons = test_bot.BotTests.buttons

    async def asyncSetUp(self):
        await test_bot.BotTests.asyncSetUp(self)
        for user in (101, 202):
            self.db.upsert_user(user, f"User{user}")
        self.clock = NOW
        for target in ("reminder_scheduler.now", "reminder_handlers.now"):
            patcher = patch(target, side_effect=lambda: self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send_error = None
        original = self.request.do_request

        async def do_request(url, method, request_data=None, **kwargs):
            if self.send_error and url.endswith("/sendMessage"):
                self.request.calls.append(("sendMessage-failed", {}))
                raise self.send_error
            return await original(url, method, request_data, **kwargs)

        self.request.do_request = do_request

    # ---------- helpers ----------

    def callbacks(self):
        return [b["callback_data"] for b in self.buttons()]

    def callback(self, part):
        return next(data for data in self.callbacks() if part in data)

    def state(self, user=101):
        return self.app.user_data[user].get(STATE_KEY)

    def jobs(self, reminder_id):
        return list(self.app.job_queue.get_jobs_by_name(f"reminder:{reminder_id}"))

    def reminder(self, reminder_id, user=101):
        return store.get_reminder(self.db, user, reminder_id)

    def deliveries(self):
        return [params for method, params in self.request.calls
                if method == "sendMessage" and params["text"].startswith("⏰ Напоминание")]

    async def run_job(self, reminder_id):
        jobs = self.jobs(reminder_id)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        job.schedule_removal()  # a date job leaves the queue when it fires
        await job.run(self.app)
        return job

    async def create(self, text="Позвонить маме", day=None, at="18:30", repeat="once", zone="Europe/Moscow",
                     user=101):
        if store.get_timezone(self.db, user) != zone:
            await self.send(f"/timezone {zone}", user=user)
        await self.send("/remind", user=user)
        await self.send(text, user=user)
        if day is None:
            await self.click(self.callback("date:2026-10-15"), user=user)
        else:
            await self.send(day, user=user)
        await self.send(at, user=user)
        await self.click(self.callback(f"repeat:{repeat}"), user=user)
        if repeat == "weekdays":
            for n in (0, 2):
                await self.click(self.callback(f"day:{n}"), user=user)
            await self.click(self.callback("days:ok"), user=user)
        await self.click(self.callback("confirm:ok"), user=user)
        return int(re.search(r"Напоминание #(\d+) создано", self.last_message()["text"])[1])

    def add_direct(self, repeat="once", next_run=None, zone="Europe/Moscow", user=101, day="2026-10-15", at="18:30"):
        schedule = Schedule(zone, date.fromisoformat(day), time.fromisoformat(at), 0, repeat,
                            frozenset({0}) if repeat in ("weekly", "weekdays") else frozenset())
        return store.create_reminder(self.db, user, "Прямое", schedule, next_run or NOW + timedelta(hours=1))

    # ---------- menu and timezone ----------

    async def test_menu_button_shows_reminder_menu(self):
        await self.send("⏰ Напоминания")
        self.assertEqual(self.callbacks(), [
            "rem:add", "rem:list:0", "rem:rep:0", "rem:edit_list:0", "rem:delete_list:0", "rem:tz", "home",
        ])
        self.assertIn("не выбран", self.last_message()["text"])

    async def test_remind_asks_for_timezone_first_then_continues(self):
        await self.send("/remind")
        self.assertIn("часовой пояс", self.last_message()["text"])
        await self.click("rem:tzset:c:Europe/Moscow")
        self.assertEqual(store.get_timezone(self.db, 101), "Europe/Moscow")
        self.assertEqual(self.state()["step"], "text")

    async def test_timezone_validation_and_manual_input(self):
        await self.send("/timezone Moscow")
        self.assertIn("Не знаю", self.last_message()["text"])
        self.assertIsNone(store.get_timezone(self.db, 101))
        await self.send("europe/warsaw")
        self.assertEqual(store.get_timezone(self.db, 101), "Europe/Warsaw")
        self.assertIsNone(self.state())

    async def test_timezone_change_does_not_move_reminders(self):
        reminder_id = await self.create()
        before = self.reminder(reminder_id)
        await self.send("/timezone Europe/Berlin")
        self.assertIn("остаются", self.last_message()["text"])
        after = self.reminder(reminder_id)
        self.assertEqual((after.timezone, after.next_run), ("Europe/Moscow", before.next_run))
        await self.send(f"/editremind {reminder_id}")
        self.assertIn("(Europe/Moscow)", self.last_message()["text"])

    # ---------- creation dialog ----------

    async def test_create_once_and_deliver(self):
        reminder_id = await self.create()
        reminder = self.reminder(reminder_id)
        self.assertEqual(reminder.next_run, datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc))
        self.assertEqual((reminder.local_time, reminder.timezone, reminder.status), ("18:30", "Europe/Moscow", "active"))
        self.assertIn("создано", self.last_message()["text"])
        self.assertIsNone(self.state())
        self.clock = reminder.next_run
        await self.run_job(reminder_id)
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.deliveries()[0]["chat_id"], 101)
        self.assertIn("Позвонить маме", self.deliveries()[0]["text"])
        self.assertEqual(self.reminder(reminder_id).status, "done")
        self.assertEqual(self.jobs(reminder_id), [])

    async def test_summary_before_confirmation(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind Полить цветы")
        await self.send("завтра")
        await self.send("09:00")
        await self.click(self.callback("repeat:daily"))
        text = self.last_message()["text"]
        self.assertIn("Полить цветы", text)
        self.assertIn("16.10.2026 09:00", text)
        self.assertIn("каждый день", text)
        self.assertEqual(store.list_reminders(self.db, 101).total, 0)

    async def test_invalid_input_repeats_current_step(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind")
        await self.send("а" * 501)
        self.assertEqual(self.state()["step"], "text")
        await self.send("Текст")
        for bad in ("32.13", "14.10.2026", "когда-нибудь"):
            await self.send(bad)
            self.assertEqual(self.state()["step"], "date")
            self.assertIn("⚠️", self.last_message()["text"])
        await self.send("15.10.2026")
        for bad in ("25:00", "14:00", "утром"):  # 14:00 Moscow has already passed
            await self.send(bad)
            self.assertEqual(self.state()["step"], "time")
        await self.send("18:00")
        self.assertEqual(self.state()["step"], "repeat")
        await self.send("каждый день")
        self.assertEqual(self.state()["step"], "repeat")
        self.assertIn("кнопкой", self.last_message()["text"])
        self.assertEqual(self.state()["draft"]["text"], "Текст")

    async def test_cancel_at_every_step(self):
        await self.send("/timezone Europe/Moscow")
        for inputs in ([], ["Текст"], ["Текст", "завтра"], ["Текст", "завтра", "10:00"]):
            with self.subTest(steps=len(inputs)):
                await self.send("/remind")
                for text in inputs:
                    await self.send(text)
                await self.send("/cancel")
                self.assertIsNone(self.state())
                self.assertIn("отменено", self.last_message()["text"])
        await self.send("/remind")
        await self.click("rem:cancel")
        self.assertIsNone(self.state())
        self.assertEqual(store.list_reminders(self.db, 101).total, 0)

    async def test_selected_weekdays(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind Спортзал")
        await self.send("завтра")
        await self.send("19:00")
        await self.click(self.callback("repeat:weekdays"))
        await self.click(self.callback("days:ok"))
        self.assertIn("хотя бы один", self.last_message()["text"])
        for n in (0, 2, 2, 4):
            await self.click(self.callback(f"day:{n}"))
        await self.click(self.callback("days:ok"))
        await self.click(self.callback("confirm:ok"))
        reminder = store.list_reminders(self.db, 101).reminders[0]
        self.assertEqual((reminder.repeat, reminder.repeat_days), ("weekdays", "0,4"))
        self.assertEqual(reminder.next_run.astimezone(timezone(timedelta(hours=3))).weekday(), 4)  # Fri 16.10

    async def test_weekly_and_monthly_keep_date_parameters(self):
        weekly = await self.create("Неделя", "20.10.2026", "10:00", "weekly")
        monthly = await self.create("Месяц", "31.10.2026", "10:00", "monthly")
        self.assertEqual(self.reminder(weekly).repeat_days, "1")  # Tuesday
        self.clock = self.reminder(monthly).next_run
        await self.run_job(monthly)
        self.assertEqual(self.reminder(monthly).next_run.date(), date(2026, 11, 30))

    async def test_dst_ambiguous_time_offers_choice(self):
        await self.send("/timezone Europe/Berlin")
        await self.send("/remind Ночь")
        await self.send("25.10.2026")
        await self.send("02:30")
        self.assertEqual(self.state()["step"], "fold")
        self.assertIn("дважды", self.last_message()["text"])
        await self.click(self.callback("fold:1"))
        await self.click(self.callback("repeat:once"))
        await self.click(self.callback("confirm:ok"))
        reminder = store.list_reminders(self.db, 101).reminders[0]
        self.assertEqual(reminder.next_run, datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(reminder.fold, 1)

    async def test_dst_nonexistent_time_is_rejected(self):
        await self.send("/timezone Europe/Berlin")
        await self.send("/remind Весна")
        await self.send("28.03.2027")
        await self.send("02:30")
        self.assertEqual(self.state()["step"], "time")
        self.assertIn("нет", self.last_message()["text"])
        await self.send("03:30")
        self.assertEqual(self.state()["step"], "repeat")

    async def test_stale_dialog_buttons(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind Первый")
        old_today = self.callback("date:2026-10-15")
        await self.send("/cancel")
        await self.click(old_today)
        self.assertIn("завершённому", self.last_message()["text"])
        reminder_id = await self.create()
        await self.click(old_today)
        await self.click("rem:v:00000000:confirm:ok")
        await self.click("rem:v:zz:repeat:never")
        self.assertEqual(store.list_reminders(self.db, 101).total, 1)
        self.assertEqual(len(self.jobs(reminder_id)), 1)
        for data in ("rem:nonsense", "rem:list:-1", "rem:open:9223372036854775808:list:0", "rem:open:1:evil:0"):
            await self.click(data)
            self.assertIn("/reminders", self.last_message()["text"])

    # ---------- editing ----------

    async def test_edit_text_date_time_and_repeat(self):
        reminder_id = await self.create()
        await self.send(f"/editremind {reminder_id}")
        await self.click(self.callback("e_text"))
        await self.send("Новый текст")
        await self.click(self.callback("e_date"))
        await self.send("20.10.2026")
        await self.click(self.callback("e_time"))
        await self.send("08:15")
        await self.click(self.callback("e_repeat"))
        await self.click(self.callback("repeat:hourly"))
        reminder = self.reminder(reminder_id)
        self.assertEqual((reminder.text, reminder.local_date, reminder.local_time, reminder.repeat),
                         ("Новый текст", "2026-10-20", "08:15", "hourly"))
        self.assertEqual(reminder.next_run, datetime(2026, 10, 20, 5, 15, tzinfo=timezone.utc))
        self.assertIn("обновлено", self.last_message()["text"])
        jobs = self.jobs(reminder_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].data["version"], reminder.version)

    async def test_edit_once_to_past_is_rejected(self):
        reminder_id = await self.create()
        await self.click(f"rem:e_time:{reminder_id}:list:0")
        await self.send("10:00")
        self.assertEqual(self.state()["step"], "time")
        self.assertEqual(self.reminder(reminder_id).local_time, "18:30")

    async def test_concurrent_change_rejects_stale_edit(self):
        reminder_id = await self.create()
        await self.click(f"rem:e_text:{reminder_id}:list:0")
        reminder = self.reminder(reminder_id)
        store.update_reminder(self.db, 101, reminder_id, reminder.version, text="Изменено в другом месте")
        await self.send("Устаревшая правка")
        self.assertIn("не применено", self.last_message()["text"])
        self.assertEqual(self.reminder(reminder_id).text, "Изменено в другом месте")

    async def test_pause_resume_and_job_queue(self):
        reminder_id = await self.create(repeat="daily")
        await self.click(f"rem:pause:{reminder_id}:list:0")
        self.assertEqual(self.reminder(reminder_id).status, "paused")
        self.assertEqual(self.jobs(reminder_id), [])
        await self.click(f"rem:pause:{reminder_id}:list:0")
        self.assertIn("уже отключено", self.last_message()["text"])
        self.clock = NOW + timedelta(days=3)
        await self.click(f"rem:resume:{reminder_id}:list:0")
        reminder = self.reminder(reminder_id)
        self.assertEqual(reminder.status, "active")
        self.assertGreater(reminder.next_run, self.clock)
        self.assertEqual(len(self.jobs(reminder_id)), 1)

    async def test_resume_of_past_once_reminder_requires_new_time(self):
        reminder_id = await self.create()
        await self.click(f"rem:pause:{reminder_id}:list:0")
        self.clock = NOW + timedelta(days=1)
        await self.click(f"rem:resume:{reminder_id}:list:0")
        self.assertIn("уже прошло", self.last_message()["text"])
        self.assertEqual(self.reminder(reminder_id).status, "paused")

    async def test_old_job_does_nothing_after_edit_or_pause(self):
        reminder_id = await self.create()
        old_job = self.jobs(reminder_id)[0]
        await self.click(f"rem:e_text:{reminder_id}:list:0")
        await self.send("Новый")
        self.clock = self.reminder(reminder_id).next_run
        await old_job.run(self.app)
        self.assertEqual(self.deliveries(), [])
        await self.run_job(reminder_id)
        self.assertEqual(len(self.deliveries()), 1)

    async def test_delete_with_confirmation(self):
        reminder_id = await self.create()
        await self.send(f"/delremind {reminder_id}")
        self.assertIsNotNone(self.reminder(reminder_id))
        confirmation = self.callback("rem:confirm:")
        job = self.jobs(reminder_id)[0]
        await self.click(confirmation)
        self.assertIsNone(self.reminder(reminder_id))
        self.assertEqual(self.jobs(reminder_id), [])
        await self.click(confirmation)
        self.assertIn("устарело", self.last_message()["text"])
        await job.run(self.app)  # a job already taken by the scheduler finds no row
        self.assertEqual(self.deliveries(), [])

    async def test_delete_reject_and_cancel(self):
        reminder_id = await self.create()
        await self.click(f"rem:del:{reminder_id}:list:0")
        await self.click(self.callback("rem:reject:"))
        self.assertIn("отменено", self.last_message()["text"])
        await self.send("/delremind")
        await self.click(self.callback("rem:del:"))
        confirmation = self.callback("rem:confirm:")
        await self.send("/cancel")
        await self.click(confirmation)
        self.assertIsNotNone(self.reminder(reminder_id))

    # ---------- owner, lists ----------

    async def test_other_user_has_no_access(self):
        reminder_id = await self.create("Секрет")
        await self.send(f"/delremind {reminder_id}")
        confirmation = self.callback("rem:confirm:")
        await self.send("/timezone UTC", user=202)
        for action in ("open", "del", "pause", "resume", "e_text", "e_date"):
            await self.click(f"rem:{action}:{reminder_id}:list:0", user=202)
            self.assertNotIn("Секрет", self.last_message()["text"])
        await self.click(confirmation, user=202)
        await self.send(f"/editremind {reminder_id}", user=202)
        self.assertNotIn("Секрет", self.last_message()["text"])
        await self.send("/reminders", user=202)
        self.assertNotIn("Секрет", self.last_message()["text"])
        reminder = self.reminder(reminder_id)
        self.assertEqual((reminder.text, reminder.status), ("Секрет", "active"))

    async def test_list_pagination_and_recurring_filter(self):
        await self.send("/timezone Europe/Moscow")
        for n in range(7):
            self.add_direct("daily" if n < 2 else "once")
        await self.send("/reminders")
        self.assertIn("Страница 1/2", self.last_message()["text"])
        await self.click("rem:list:1")
        self.assertIn("Страница 2/2", self.last_message()["text"])
        await self.click("rem:rep:0")
        self.assertIn("Повторяющиеся напоминания — 2", self.last_message()["text"])
        await self.click(self.callback("rem:open:"))
        self.assertIn("rem:rep:0", self.callbacks())

    async def test_list_fits_telegram_limit(self):
        await self.send("/timezone Europe/Moscow")
        for _ in range(5):
            store.create_reminder(self.db, 101, "😀" * store.MAX_TEXT_LENGTH,
                                  Schedule("America/Argentina/ComodRivadavia", date(2026, 10, 20), time(10), 0,
                                           "weekdays", frozenset(range(7))), NOW + timedelta(days=1))
        await self.send("/reminders")
        self.assertLessEqual(len(self.last_message()["text"].encode("utf-16-le")) // 2, 4096)
        await self.click(self.callback("rem:open:"))
        self.assertTrue(all(len(data.encode()) <= 64 for data in self.callbacks()))

    async def test_group_chat_is_refused(self):
        await self.create("Секрет")
        for command in ("/reminders", "/remind x", "/editremind 1", "/delremind 1", "/timezone UTC", "⏰ Напоминания"):
            await self.send(command, chat_type="group")
            self.assertIn("личном чате", self.last_message()["text"])
            self.assertNotIn("Секрет", self.last_message()["text"])

    # ---------- scheduler ----------

    async def test_repeating_delivery_moves_to_next_moment(self):
        reminder_id = await self.create(repeat="daily")
        first = self.reminder(reminder_id).next_run
        self.clock = first + timedelta(seconds=1)
        await self.run_job(reminder_id)
        reminder = self.reminder(reminder_id)
        self.assertEqual(reminder.next_run, first + timedelta(days=1))
        self.assertEqual(reminder.status, "active")
        self.assertEqual(len(self.jobs(reminder_id)), 1)

    async def test_hourly_delivery_keeps_minute(self):
        reminder_id = await self.create(at="15:20", repeat="hourly")
        self.clock = self.reminder(reminder_id).next_run + timedelta(minutes=7)  # late job
        await self.run_job(reminder_id)
        self.assertEqual(self.reminder(reminder_id).next_run, datetime(2026, 10, 15, 13, 20, tzinfo=timezone.utc))

    async def test_restore_after_restart(self):
        overdue_once = self.add_direct("once", NOW - timedelta(days=2))
        overdue_daily = self.add_direct("daily", NOW - timedelta(days=30), day="2026-09-01", at="09:00")
        future = self.add_direct("once", NOW + timedelta(hours=2))
        paused = self.add_direct("once", NOW + timedelta(hours=3))
        store.update_reminder(self.db, 101, paused, self.reminder(paused).version, status="paused")
        self.assertEqual(reminder_scheduler.restore_jobs(self.app), 3)
        self.assertEqual(reminder_scheduler.restore_jobs(self.app), 3)  # no duplicates on a repeat
        for reminder_id in (overdue_once, overdue_daily, future):
            self.assertEqual(len(self.jobs(reminder_id)), 1)
        self.assertEqual(self.jobs(paused), [])
        daily = self.reminder(overdue_daily)
        self.assertEqual(daily.next_run, datetime(2026, 10, 16, 6, tzinfo=timezone.utc))  # skipped missed days
        await self.run_job(overdue_once)  # overdue once-reminder is sent once after start
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.reminder(overdue_once).status, "done")

    async def test_restore_runs_in_post_init(self):
        reminder_id = self.add_direct("once")
        await self.app.post_init(self.app)
        self.assertEqual(len(self.jobs(reminder_id)), 1)

    async def test_network_error_retries_then_gives_up(self):
        reminder_id = await self.create()
        self.clock = self.reminder(reminder_id).next_run
        self.send_error = NetworkError("connection reset")
        with self.assertLogs("reminder_scheduler", level="WARNING") as logs:
            for attempt in range(len(reminder_scheduler.RETRY_DELAYS)):
                await self.run_job(reminder_id)
                self.assertEqual(self.jobs(reminder_id)[0].data["retry"], attempt + 1)
                self.assertEqual(self.reminder(reminder_id).error_count, attempt + 1)
            await self.run_job(reminder_id)
        self.assertEqual(self.reminder(reminder_id).status, "failed")
        self.assertEqual(self.jobs(reminder_id), [])
        self.assertNotIn("Позвонить", "\n".join(logs.output))

    async def test_network_error_then_success(self):
        reminder_id = await self.create()
        self.clock = self.reminder(reminder_id).next_run
        self.send_error = NetworkError("timeout")
        with self.assertLogs("reminder_scheduler", level="WARNING"):
            await self.run_job(reminder_id)
        self.send_error = None
        await self.run_job(reminder_id)
        reminder = self.reminder(reminder_id)
        self.assertEqual((reminder.status, reminder.error_count), ("done", 0))
        self.assertEqual(len(self.deliveries()), 1)

    async def test_repeating_reminder_moves_on_after_retries(self):
        reminder_id = await self.create(repeat="daily")
        first = self.reminder(reminder_id).next_run
        self.clock = first
        self.send_error = NetworkError("down")
        with self.assertLogs("reminder_scheduler", level="WARNING"):
            for _ in range(len(reminder_scheduler.RETRY_DELAYS) + 1):
                await self.run_job(reminder_id)
        reminder = self.reminder(reminder_id)
        self.assertEqual((reminder.status, reminder.next_run), ("active", first + timedelta(days=1)))
        self.assertEqual(self.jobs(reminder_id)[0].data["retry"], 0)

    async def test_blocked_bot_stops_without_retries(self):
        reminder_id = await self.create(repeat="daily")
        self.clock = self.reminder(reminder_id).next_run
        self.send_error = Forbidden("bot was blocked by the user")
        with self.assertLogs("reminder_scheduler", level="WARNING"):
            await self.run_job(reminder_id)
        self.assertEqual(self.reminder(reminder_id).status, "failed")
        self.assertEqual(self.jobs(reminder_id), [])
        self.send_error = None
        await self.send(f"/editremind {reminder_id}")
        self.assertIn("не доставлено", self.last_message()["text"])

    # ---------- compatibility ----------

    async def test_sections_interrupt_each_other(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind")
        await self.send("/add")
        self.assertIsNone(self.state())
        await self.send("Задача")
        self.assertEqual(self.db.list_tasks(101).total, 1)
        await self.send("/note")
        await self.send("/remind")
        self.assertNotIn(NOTE_STATE_KEY, self.app.user_data[101])
        await self.send("Текст напоминания")
        self.assertEqual(self.state()["step"], "date")
        await self.send("📝 Заметки")
        self.assertIsNone(self.state())
        await self.send("/remind")
        await self.click("tasks:menu")
        self.assertIsNone(self.state())
        await self.send("/add")
        await self.click("rem:menu")
        self.assertNotIn(TASK_STATE_KEY, self.app.user_data[101])
        self.assertEqual(self.db.list_notes(101).total, 0)
        self.assertEqual(store.list_reminders(self.db, 101).total, 0)

    async def test_cancel_targets_active_section(self):
        await self.send("/timezone Europe/Moscow")
        await self.send("/remind")
        await self.send("/cancel")
        self.assertIn("rem:add", self.callbacks())
        await self.send("/note")
        await self.send("/cancel")
        self.assertIn("notes:add", self.callbacks())
        await self.send("/add")
        await self.send("/cancel")
        self.assertIn("tasks:add", self.callbacks())

    async def test_help_and_home(self):
        await self.send("/help")
        for command in ("/remind", "/reminders", "/editremind", "/delremind", "/timezone", "/notes", "/tasks"):
            self.assertIn(command, self.last_message()["text"])
        await self.send("/remind")
        await self.click("home")
        self.assertIsNone(self.state())
        markup = self.last_message()["reply_markup"]
        self.assertIn("⏰ Напоминания", json.dumps(json.loads(markup) if isinstance(markup, str) else markup,
                                                   ensure_ascii=False))
