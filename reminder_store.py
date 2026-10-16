"""SQLite operations for reminders and user settings.

User-facing functions take the owner's Telegram ID and filter by it. Functions used
only by the scheduler (all_active, load_for_delivery, claim/record_*) work by reminder ID.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from database import PAGE_SIZE, Database
from reminder_schedule import REPEATS, Schedule

MAX_TEXT_LENGTH = 500
UTC_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    text: str
    timezone: str
    local_date: str
    local_time: str
    fold: int
    repeat: str
    repeat_days: str
    next_run_utc: str | None
    status: str
    version: int
    created_at: str
    last_sent_at: str | None
    error_count: int
    last_error: str | None

    @property
    def schedule(self) -> Schedule:
        return Schedule(
            self.timezone, date.fromisoformat(self.local_date), time.fromisoformat(self.local_time),
            self.fold, self.repeat, frozenset(int(d) for d in self.repeat_days.split(",") if d),
        )

    @property
    def next_run(self) -> datetime | None:
        return from_db_time(self.next_run_utc)


@dataclass(frozen=True)
class ReminderPage:
    reminders: tuple[Reminder, ...]
    page: int
    pages: int
    total: int


def to_db_time(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime(UTC_FORMAT) if moment else None


def from_db_time(value: str | None) -> datetime | None:
    return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc) if value else None


def clean_text(text: str) -> str:
    text = text.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"Текст напоминания должен содержать от 1 до {MAX_TEXT_LENGTH} символов.")
    return text


def schedule_fields(schedule: Schedule) -> dict:
    if schedule.repeat not in REPEATS:
        raise ValueError("Некорректное правило повторения.")
    return {
        "timezone": schedule.timezone, "local_date": schedule.local_date.isoformat(),
        "local_time": schedule.local_time.strftime("%H:%M"), "fold": schedule.fold,
        "repeat": schedule.repeat, "repeat_days": ",".join(str(d) for d in sorted(schedule.days)),
    }


def _reminder(row) -> Reminder:
    return Reminder(
        row["id"], row["user_id"], row["text"], row["timezone"], row["local_date"], row["local_time"],
        row["fold"], row["repeat"], row["repeat_days"], row["next_run_utc"], row["status"], row["version"],
        row["created_at"], row["last_sent_at"], row["error_count"], row["last_error"],
    )


def get_timezone(db: Database, user_id: int) -> str | None:
    with db.connection() as conn:
        row = conn.execute("SELECT timezone FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row["timezone"] if row else None


def set_timezone(db: Database, user_id: int, zone: str) -> int:
    """Save the user's zone; returns how many unfinished reminders keep another zone."""
    with db.connection() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, timezone) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone, updated_at = CURRENT_TIMESTAMP
        """, (user_id, zone))
        return conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND timezone != ? AND status IN ('active', 'paused')",
            (user_id, zone),
        ).fetchone()[0]


def create_reminder(db: Database, user_id: int, text: str, schedule: Schedule, next_run: datetime) -> int:
    fields = {"text": clean_text(text), **schedule_fields(schedule), "next_run_utc": to_db_time(next_run)}
    columns = ", ".join(fields)
    with db.connection() as conn:
        return conn.execute(
            f"INSERT INTO reminders (user_id, {columns}) VALUES (?, {', '.join('?' * len(fields))})",
            (user_id, *fields.values()),
        ).lastrowid


def get_reminder(db: Database, user_id: int, reminder_id: int) -> Reminder | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND id = ?", (user_id, reminder_id),
        ).fetchone()
        return _reminder(row) if row else None


def list_reminders(db: Database, user_id: int, page: int = 0, recurring_only: bool = False) -> ReminderPage:
    where = "user_id = ?" + (" AND repeat != 'once'" if recurring_only else "")
    with db.connection() as conn:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM reminders WHERE {where}", (user_id,)).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = conn.execute(f"""
            SELECT * FROM reminders WHERE {where}
            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END,
                     CASE WHEN status = 'active' THEN next_run_utc END, id DESC
            LIMIT ? OFFSET ?
        """, (user_id, PAGE_SIZE, page * PAGE_SIZE)).fetchall()
        return ReminderPage(tuple(_reminder(row) for row in rows), page, pages, total)


# Fields a user edit may change; column names never come from user input.
EDITABLE = frozenset({
    "text", "timezone", "local_date", "local_time", "fold", "repeat", "repeat_days", "next_run_utc", "status",
})


def update_reminder(db: Database, user_id: int, reminder_id: int, version: int, **changes) -> bool:
    """Apply a user edit only if nobody changed the reminder since `version` was read."""
    if not changes or not set(changes) <= EDITABLE:
        raise ValueError("Некорректные изменения.")
    if "text" in changes:
        changes["text"] = clean_text(changes["text"])
    if changes.get("status") == "active":
        changes["error_count"] = 0
    assignments = ", ".join(f"{column} = ?" for column in changes)
    with db.connection() as conn:
        return conn.execute(f"""
            UPDATE reminders SET {assignments}, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND id = ? AND version = ?
        """, (*changes.values(), user_id, reminder_id, version)).rowcount == 1


def delete_reminder(db: Database, user_id: int, reminder_id: int) -> bool:
    with db.connection() as conn:
        return conn.execute(
            "DELETE FROM reminders WHERE user_id = ? AND id = ?", (user_id, reminder_id),
        ).rowcount == 1


def all_active(db: Database) -> list[Reminder]:
    with db.connection() as conn:
        return [_reminder(row) for row in conn.execute("SELECT * FROM reminders WHERE status = 'active'")]


def load_for_delivery(db: Database, reminder_id: int) -> Reminder | None:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _reminder(row) if row else None


def claim_delivery(db: Database, reminder_id: int, version: int) -> bool:
    """Record an attempt; False if the reminder was edited, disabled or deleted meanwhile."""
    with db.connection() as conn:
        return conn.execute("""
            UPDATE reminders SET last_attempt_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ? AND status = 'active'
        """, (reminder_id, version)).rowcount == 1


def record_success(db: Database, reminder_id: int, version: int, next_run: datetime | None) -> bool:
    """Once reminders become 'done'; repeating ones move to their next moment."""
    with db.connection() as conn:
        return conn.execute("""
            UPDATE reminders SET last_sent_at = CURRENT_TIMESTAMP, error_count = 0, last_error = NULL,
                status = CASE WHEN ? IS NULL THEN 'done' ELSE status END,
                next_run_utc = COALESCE(?, next_run_utc)
            WHERE id = ? AND version = ? AND status = 'active'
        """, (to_db_time(next_run), to_db_time(next_run), reminder_id, version)).rowcount == 1


def record_failure(
    db: Database, reminder_id: int, version: int, error: str, *, failed: bool = False,
    next_run: datetime | None = None,
) -> bool:
    """Count an error (type name only, never message text); optionally stop or move on."""
    with db.connection() as conn:
        return conn.execute("""
            UPDATE reminders SET error_count = error_count + 1, last_error = ?,
                status = CASE WHEN ? THEN 'failed' ELSE status END,
                next_run_utc = COALESCE(?, next_run_utc)
            WHERE id = ? AND version = ? AND status = 'active'
        """, (error, failed, to_db_time(next_run), reminder_id, version)).rowcount == 1


def move_next_run(db: Database, reminder_id: int, version: int, next_run: datetime) -> bool:
    with db.connection() as conn:
        return conn.execute(
            "UPDATE reminders SET next_run_utc = ? WHERE id = ? AND version = ? AND status = 'active'",
            (to_db_time(next_run), reminder_id, version),
        ).rowcount == 1
