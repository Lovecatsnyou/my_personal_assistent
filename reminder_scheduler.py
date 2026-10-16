"""JobQueue planning and delivery of reminders.

One job per reminder, named "reminder:<id>"; rescheduling replaces it, so a process never
holds duplicates. A job carries the reminder version: if the user edits, disables or
deletes the reminder, the old job finds a different version (or no row) and does nothing.
Delivery is at-least-once: if the process dies after Telegram accepted the message but
before SQLite recorded it, the reminder is sent again after restart.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import sqlite3

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes

from database import Database
from reminder_schedule import next_run
import reminder_store as store

logger = logging.getLogger(__name__)
RETRY_DELAYS = (30, 120, 600)  # seconds; then a once-reminder fails, a repeating one moves on


def job_name(reminder_id: int) -> str:
    return f"reminder:{reminder_id}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def cancel_job(application: Application, reminder_id: int) -> None:
    for job in application.job_queue.get_jobs_by_name(job_name(reminder_id)):
        job.schedule_removal()


def _run_at(application: Application, reminder_id: int, version: int, when: datetime | float, retry: int = 0) -> None:
    cancel_job(application, reminder_id)
    application.job_queue.run_once(
        deliver, when, name=job_name(reminder_id),
        data={"id": reminder_id, "version": version, "retry": retry},
    )


def sync_job(application: Application, reminder: store.Reminder | None, reminder_id: int) -> None:
    """Make the queue match SQLite: one job for an active reminder, none otherwise."""
    if reminder is None or reminder.status != "active" or reminder.next_run is None:
        cancel_job(application, reminder_id)
        return
    # An overdue moment (e.g. a once-reminder missed while the bot was off) runs right away.
    when = reminder.next_run if reminder.next_run > now() else 1.0
    _run_at(application, reminder.id, reminder.version, when)


def restore_jobs(application: Application) -> int:
    """At startup: schedule active reminders; repeating ones skip moments missed while offline."""
    database: Database = application.bot_data["database"]
    count = 0
    for reminder in store.all_active(database):
        if reminder.repeat != "once" and (reminder.next_run is None or reminder.next_run <= now()):
            upcoming = next_run(reminder.schedule, now())
            if not store.move_next_run(database, reminder.id, reminder.version, upcoming):
                continue
            reminder = store.load_for_delivery(database, reminder.id)
        sync_job(application, reminder, reminder.id)
        count += 1
    return count


async def restore_on_start(application: Application) -> None:
    # Runs once before polling; JobQueue must be touched from the event loop thread, not to_thread.
    count = restore_jobs(application)
    logger.info("Восстановлено напоминаний: %s", count)


async def deliver(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    reminder_id, version, retry = data["id"], data["version"], data["retry"]
    database: Database = context.application.bot_data["database"]
    reminder = await asyncio.to_thread(store.load_for_delivery, database, reminder_id)
    if reminder is None or reminder.version != version or reminder.status != "active":
        return
    if not await asyncio.to_thread(store.claim_delivery, database, reminder_id, version):
        return
    upcoming = None if reminder.repeat == "once" else next_run(reminder.schedule, max(now(), reminder.next_run))
    try:
        await context.bot.send_message(reminder.user_id, f"⏰ Напоминание\n\n{reminder.text}")
    except (Forbidden, BadRequest) as exc:
        # Blocked bot, deleted chat and similar: retrying cannot help.
        logger.warning("Напоминание #%s не доставлено: %s", reminder_id, type(exc).__name__)
        await asyncio.to_thread(store.record_failure, database, reminder_id, version, type(exc).__name__, failed=True)
        return
    except (NetworkError, RetryAfter, TelegramError) as exc:
        await _retry_or_give_up(context.application, reminder, retry, exc, upcoming)
        return
    logger.info("Напоминание #%s отправлено", reminder_id)
    try:
        recorded = await asyncio.to_thread(store.record_success, database, reminder_id, version, upcoming)
    except sqlite3.Error as exc:
        # The message is already delivered; do not resend it now. The next restart reschedules.
        logger.error("Не удалось сохранить отправку напоминания #%s: %s", reminder_id, type(exc).__name__)
        return
    if recorded and upcoming is not None:
        _run_at(context.application, reminder_id, version, upcoming)


async def _retry_or_give_up(
    application: Application, reminder: store.Reminder, retry: int, exc: Exception, upcoming: datetime | None,
) -> None:
    database: Database = application.bot_data["database"]
    name = type(exc).__name__
    if retry < len(RETRY_DELAYS):
        delay = RETRY_DELAYS[retry]
        if isinstance(exc, RetryAfter):
            retry_after = exc.retry_after
            delay = max(delay, retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after)
        logger.warning("Напоминание #%s: временная ошибка %s, повтор через %s с", reminder.id, name, delay)
        if await asyncio.to_thread(store.record_failure, database, reminder.id, reminder.version, name):
            _run_at(application, reminder.id, reminder.version, float(delay), retry + 1)
        return
    logger.warning("Напоминание #%s: попытки исчерпаны (%s)", reminder.id, name)
    if upcoming is None:
        await asyncio.to_thread(store.record_failure, database, reminder.id, reminder.version, name, failed=True)
    elif await asyncio.to_thread(
        store.record_failure, database, reminder.id, reminder.version, name, next_run=upcoming,
    ):
        _run_at(application, reminder.id, reminder.version, upcoming)
