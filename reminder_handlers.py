"""Reminder commands, dialogs and inline menus; dialog state lives in user_data["reminders_state"].

Dialog buttons carry a per-dialog nonce, so buttons of a finished or replaced dialog are
rejected. Edits are saved only if the reminder version has not changed since the edit began.
"""

import asyncio
from datetime import date, datetime, time, timedelta
import re
import secrets
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from reminder_schedule import (
    WEEKDAYS, Schedule, describe, find_timezone, format_local, local_status, next_run, parse_date,
    parse_time, to_utc, utc_offset,
)
from reminder_scheduler import cancel_job, now, sync_job
import reminder_store as store
from task_handlers import get_database, parse_id, respond

REMINDERS_BUTTON = "⏰ Напоминания"
STATE_KEY = "reminders_state"
REMINDER_COMMANDS = frozenset({"remind", "reminders", "editremind", "delremind", "timezone"})
POPULAR_ZONES = (
    "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara", "Asia/Yekaterinburg", "Europe/Minsk",
    "Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin", "Europe/London", "Asia/Almaty", "UTC",
)
REPEAT_TITLES = {
    "once": "Без повторения", "daily": "Каждый день", "weekly": "Каждую неделю",
    "monthly": "Каждый месяц", "weekdays": "По дням недели", "hourly": "Каждый час",
}
STATUS_ICONS = {"active": "⏰", "paused": "⏸", "done": "✅", "failed": "⚠️"}
STATUS_TITLES = {
    "active": "активно", "paused": "отключено", "done": "выполнено",
    "failed": "не доставлено (бот заблокирован или ошибка Telegram)",
}
BACK = r":(list|rep|edit_list|delete_list):([0-9]{1,10})"


def clear_reminder_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(STATE_KEY, None)


def menu_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("➕ Создать напоминание", callback_data="rem:add")],
        [InlineKeyboardButton("📋 Мои напоминания", callback_data="rem:list:0")],
        [InlineKeyboardButton("🔁 Повторяющиеся", callback_data="rem:rep:0")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="rem:edit_list:0")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="rem:delete_list:0")],
        [InlineKeyboardButton("🌍 Часовой пояс", callback_data="rem:tz")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="home")],
    ]


def reminder_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(menu_rows())


def cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✖️ Отмена", callback_data="rem:cancel")]


async def private_chat(update: Update) -> bool:
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Напоминания доступны только в личном чате с ботом.")
    return False


def db(context: ContextTypes.DEFAULT_TYPE):
    return get_database(context)


def shorten(text: str, length: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[:length - 1] + "…"


def draft_schedule(draft: dict) -> Schedule:
    day, repeat = date.fromisoformat(draft["date"]), draft.get("repeat", "once")
    # A weekly reminder repeats on the weekday of its date, also after the date is edited.
    days = {day.weekday()} if repeat == "weekly" else set(draft.get("days", ())) if repeat == "weekdays" else set()
    return Schedule(draft["tz"], day, time.fromisoformat(draft["time"]), draft.get("fold", 0), repeat, frozenset(days))


def local_today(zone: str) -> date:
    return now().astimezone(ZoneInfo(zone)).date()


# ---------- Menu, lists and card ----------

async def show_reminder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    if not await private_chat(update):
        return
    clear_reminder_state(context)
    zone = await asyncio.to_thread(store.get_timezone, db(context), update.effective_user.id)
    lines = [notice] if notice else []
    lines.append("⏰ Напоминания: выбери действие.")
    lines.append(f"🌍 Часовой пояс: {zone}" if zone else "🌍 Часовой пояс ещё не выбран — нажми «Часовой пояс».")
    await respond(update, "\n\n".join(lines), reminder_menu())


def when_text(reminder: store.Reminder, user_zone: str | None) -> str:
    suffix = f" ({reminder.timezone})" if reminder.timezone != user_zone else ""
    if reminder.status == "done" and reminder.last_sent_at:
        return f"отправлено {format_local(store.from_db_time(reminder.last_sent_at), reminder.timezone)}{suffix}"
    if reminder.next_run is None:
        return "время не задано"
    return f"{format_local(reminder.next_run, reminder.timezone)}{suffix}"


async def show_reminders(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "list", page: int = 0, notice: str = "",
) -> None:
    user_id = update.effective_user.id
    result = await asyncio.to_thread(store.list_reminders, db(context), user_id, page, mode == "rep")
    zone = await asyncio.to_thread(store.get_timezone, db(context), user_id)
    title = "🔁 Повторяющиеся напоминания" if mode == "rep" else "⏰ Мои напоминания"
    lines = [notice] if notice else []
    lines.append(f"{title} — {result.total}. Страница {result.page + 1}/{result.pages}")
    if not result.reminders:
        lines.append("Напоминаний пока нет. Нажми «Создать напоминание» или используй /remind.")
    elif mode == "edit_list":
        lines.append("Выбери напоминание для редактирования:")
    elif mode == "delete_list":
        lines.append("Выбери напоминание для удаления:")
    action = "del" if mode == "delete_list" else "open"
    icon = {"edit_list": "✏️", "delete_list": "🗑"}.get(mode)
    buttons = []
    for reminder in result.reminders:
        status_icon = STATUS_ICONS[reminder.status]
        lines.append(f"{status_icon} #{reminder.id} — {shorten(reminder.text, 80)}\n"
                     f"🕒 {when_text(reminder, zone)} · {describe(reminder.schedule)}")
        buttons.append([InlineKeyboardButton(
            f"{icon or status_icon} #{reminder.id} {shorten(reminder.text, 30)}",
            callback_data=f"rem:{action}:{reminder.id}:{mode}:{result.page}",
        )])
    navigation = []
    if result.page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data=f"rem:{mode}:{result.page - 1}"))
    if result.page + 1 < result.pages:
        navigation.append(InlineKeyboardButton("➡️", callback_data=f"rem:{mode}:{result.page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("➕ Создать напоминание", callback_data="rem:add")])
    buttons.append([InlineKeyboardButton("⏰ Меню напоминаний", callback_data="rem:menu")])
    await respond(update, "\n\n".join(lines), InlineKeyboardMarkup(buttons))


async def show_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reminder_id: int, mode: str = "list", page: int = 0,
    notice: str = "",
) -> None:
    user_id = update.effective_user.id
    reminder = await asyncio.to_thread(store.get_reminder, db(context), user_id, reminder_id)
    if reminder is None:
        await show_reminders(update, context, mode, page, "Напоминание не найдено — возможно, оно удалено.")
        return
    zone = await asyncio.to_thread(store.get_timezone, db(context), user_id)
    back = f"{mode}:{page}"
    target = f"{reminder.id}:{back}"
    toggle = (InlineKeyboardButton("⏸ Отключить", callback_data=f"rem:pause:{target}")
              if reminder.status == "active"
              else InlineKeyboardButton("▶️ Включить", callback_data=f"rem:resume:{target}"))
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Текст", callback_data=f"rem:e_text:{target}"),
         InlineKeyboardButton("📅 Дата", callback_data=f"rem:e_date:{target}"),
         InlineKeyboardButton("🕒 Время", callback_data=f"rem:e_time:{target}")],
        [InlineKeyboardButton("🔁 Повторение", callback_data=f"rem:e_repeat:{target}"), toggle],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"rem:del:{target}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data=f"rem:{back}"),
         InlineKeyboardButton("⏰ Меню", callback_data="rem:menu")],
    ])
    lines = [notice] if notice else []
    lines.append(f"{STATUS_ICONS[reminder.status]} Напоминание #{reminder.id}\n\n{reminder.text}")
    details = [
        f"Ближайшее: {when_text(reminder, zone)}" if reminder.status != "done" else f"Статус: {when_text(reminder, zone)}",
        f"Повторение: {describe(reminder.schedule)}",
        f"Часовой пояс напоминания: {reminder.timezone}",
        f"Статус: {STATUS_TITLES[reminder.status]}",
    ]
    if reminder.last_sent_at:
        details.append(f"Последняя отправка: {format_local(store.from_db_time(reminder.last_sent_at), reminder.timezone)}")
    if reminder.error_count:
        details.append(f"Ошибок доставки подряд: {reminder.error_count}")
    lines.append("\n".join(details))
    await respond(update, "\n\n".join(lines), markup)


# ---------- Timezone ----------

def timezone_markup(then: str = "") -> InlineKeyboardMarkup:
    suffix = ":c" if then == "create" else ""
    rows, row = [], []
    for zone in POPULAR_ZONES:
        if find_timezone(zone):
            row.append(InlineKeyboardButton(zone, callback_data=f"rem:tzset{suffix}:{zone}"))
            if len(row) == 2:
                rows.append(row)
                row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ Ввести вручную", callback_data=f"rem:tzinput{suffix}")])
    rows.append([InlineKeyboardButton("⏰ Меню напоминаний", callback_data="rem:menu")])
    return InlineKeyboardMarkup(rows)


async def show_timezone_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, then: str = "", notice: str = "") -> None:
    zone = await asyncio.to_thread(store.get_timezone, db(context), update.effective_user.id)
    current = f"Сейчас выбран: {zone}." if zone else "Часовой пояс ещё не выбран."
    lines = [notice] if notice else []
    lines.append(f"🌍 {current}\nВыбери пояс кнопкой или введи IANA-название, например Europe/Moscow, "
                 "Europe/Warsaw, Europe/Berlin или UTC.")
    await respond(update, "\n\n".join(lines), timezone_markup(then))


async def save_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, then: str = "") -> None:
    zone = find_timezone(text)
    if zone is None:
        context.user_data[STATE_KEY] = {"action": "tz", "then": then}
        await respond(update, "Не знаю такого часового пояса. Нужно IANA-название, например Europe/Moscow, "
                              "Europe/Berlin или UTC. Попробуй ещё раз или нажми /cancel.",
                      InlineKeyboardMarkup([cancel_row()]))
        return
    clear_reminder_state(context)
    others = await asyncio.to_thread(store.set_timezone, db(context), update.effective_user.id, zone)
    lines = [f"🌍 Часовой пояс сохранён: {zone}. Сейчас там {now().astimezone(ZoneInfo(zone)):%d.%m.%Y %H:%M}."]
    if others:
        lines.append(f"Уже созданные напоминания ({others}) остаются в своём прежнем поясе и сработают "
                     "в то же время, что и раньше. Пояс указан в их карточках.")
    if then == "create":
        await begin_create(update, context, notice="\n\n".join(lines))
    else:
        await respond(update, "\n\n".join(lines), reminder_menu())


# ---------- Dialog steps ----------

def new_state(action: str, **values) -> dict:
    return {"action": action, "nonce": secrets.token_hex(4), **values}


def step_button(state: dict, label: str, value: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=f"rem:v:{state['nonce']}:{value}")


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, step: str, notice: str = "") -> None:
    state["step"] = step
    context.user_data[STATE_KEY] = state
    draft = state["draft"]
    rows = []
    lines = [notice] if notice else []
    if step == "text":
        lines.append(f"Введи текст напоминания (1–{store.MAX_TEXT_LENGTH} символов).")
    elif step == "date":
        today = local_today(draft["tz"])
        lines.append(f"Выбери дату или введи её: ДД.ММ.ГГГГ или ДД.ММ. Часовой пояс: {draft['tz']}.")
        rows.append([step_button(state, f"{label} {today + timedelta(days=shift):%d.%m}",
                                 f"date:{(today + timedelta(days=shift)).isoformat()}")
                     for shift, label in ((0, "Сегодня"), (1, "Завтра"), (2, "Послезавтра"))])
    elif step == "time":
        local_now = now().astimezone(ZoneInfo(draft["tz"]))
        lines.append(f"Дата: {date.fromisoformat(draft['date']):%d.%m.%Y}. Введи время ЧЧ:ММ, например 09:30.\n"
                     f"Часовой пояс: {draft['tz']}, сейчас там {local_now:%d.%m %H:%M}.")
    elif step == "fold":
        day, moment = date.fromisoformat(draft["date"]), time.fromisoformat(draft["time"])
        lines.append(f"{day:%d.%m.%Y} время {moment:%H:%M} наступает дважды из-за перехода на зимнее время. "
                     "Выбери нужный вариант:")
        rows.append([step_button(state, f"Первое ({utc_offset(day, moment, draft['tz'], 0)})", "fold:0"),
                     step_button(state, f"Второе ({utc_offset(day, moment, draft['tz'], 1)})", "fold:1")])
    elif step == "repeat":
        day = date.fromisoformat(draft["date"])
        lines.append("Как часто напоминать?")
        labels = dict(REPEAT_TITLES, weekly=f"Каждую неделю ({WEEKDAYS[day.weekday()]})",
                      monthly=f"Каждый месяц ({day.day}-го)")
        rows += [[step_button(state, labels[kind], f"repeat:{kind}")] for kind in REPEAT_TITLES]
    elif step == "days":
        chosen = set(draft.get("days", ()))
        lines.append("Отметь дни недели и нажми «Готово».")
        rows.append([step_button(state, f"{'✅' if n in chosen else ''}{name}", f"day:{n}")
                     for n, name in enumerate(WEEKDAYS)])
        rows.append([step_button(state, "Готово", "days:ok")])
    elif step == "confirm":
        schedule = draft_schedule(draft)
        first = next_run(schedule, now())
        lines.append("Проверь напоминание:\n\n"
                     f"📝 {draft['text']}\n"
                     f"🕒 Первое: {format_local(first, draft['tz']) if first else '—'} ({draft['tz']})\n"
                     f"🔁 {describe(schedule)}")
        rows.append([step_button(state, "✅ Создать", "confirm:ok")])
    rows.append(cancel_row())
    await respond(update, "\n\n".join(lines), InlineKeyboardMarkup(rows))


async def begin_create(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "", notice: str = "") -> None:
    zone = await asyncio.to_thread(store.get_timezone, db(context), update.effective_user.id)
    if zone is None:
        context.user_data[STATE_KEY] = {"action": "tz", "then": "create"}
        await show_timezone_picker(update, context, "create", "Сначала выбери свой часовой пояс — "
                                                              "по нему будут вводиться дата и время.")
        return
    state = new_state("create", draft={"tz": zone})
    if text:
        await accept(update, context, state, "text", text, notice)
    else:
        await ask(update, context, state, "text", notice)


async def begin_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reminder_id: int, field: str, mode: str, page: int,
) -> None:
    reminder = await asyncio.to_thread(store.get_reminder, db(context), update.effective_user.id, reminder_id)
    if reminder is None:
        await show_reminders(update, context, mode, page, "Напоминание не найдено — возможно, оно удалено.")
        return
    draft = {
        "tz": reminder.timezone, "text": reminder.text, "date": reminder.local_date, "time": reminder.local_time,
        "fold": reminder.fold, "repeat": reminder.repeat, "days": sorted(reminder.schedule.days),
    }
    state = new_state("edit", draft=draft, id=reminder.id, version=reminder.version, mode=mode, page=page,
                      status=reminder.status)
    current = {
        "text": f"Сейчас: {reminder.text}",
        "date": f"Сейчас: {date.fromisoformat(reminder.local_date):%d.%m.%Y} (время {reminder.local_time}).",
        "time": f"Сейчас: {reminder.local_time}.",
        "repeat": f"Сейчас: {describe(reminder.schedule)}.",
    }[field]
    await ask(update, context, state, field, f"Изменение напоминания #{reminder.id}. {current}")


async def accept(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, step: str, value: str, notice: str = "",
) -> None:
    """Validate one dialog step; on error repeat the same step with an explanation."""
    draft, creating = state["draft"], state["action"] == "create"
    retry = lambda message: ask(update, context, state, step, f"⚠️ {message}")  # noqa: E731
    if step == "text":
        try:
            draft["text"] = store.clean_text(value)
        except ValueError as exc:
            await retry(str(exc))
            return
        if creating:
            await ask(update, context, state, "date", notice)
        else:
            await save_edit(update, context, state, step)
        return
    if step == "date":
        try:
            day = parse_date(value, local_today(draft["tz"]))
        except ValueError as exc:
            await retry(str(exc))
            return
        if creating and day < local_today(draft["tz"]):
            await retry("Эта дата уже прошла.")
            return
        draft["date"] = day.isoformat()
        if creating:
            await ask(update, context, state, "time")
        else:
            await check_moment(update, context, state, step)
        return
    if step == "time":
        try:
            draft["time"] = parse_time(value).strftime("%H:%M")
        except ValueError as exc:
            await retry(str(exc))
            return
        await check_moment(update, context, state, step)
        return
    if step == "fold":
        draft["fold"] = int(value)
        await after_moment(update, context, state, state.get("origin", "time"))
        return
    if step == "repeat":
        if value == "weekdays":
            # Editing keeps the previously chosen days preselected.
            if draft.get("repeat") != "weekdays":
                draft["days"] = []
            draft["repeat"] = value
            await ask(update, context, state, "days")
            return
        draft["repeat"] = value
        await after_repeat(update, context, state)
        return
    if step == "day":
        days = set(draft.get("days", ()))
        days ^= {int(value)}
        draft["days"] = sorted(days)
        await ask(update, context, state, "days")
        return
    if step == "days":
        if not draft.get("days"):
            await ask(update, context, state, "days", "⚠️ Выбери хотя бы один день.")
            return
        await after_repeat(update, context, state)
        return
    if step == "confirm":
        await save_new(update, context, state)


async def check_moment(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, step: str) -> None:
    """Reject times skipped by DST; ask which one is meant when the time occurs twice."""
    draft = state["draft"]
    day, moment = date.fromisoformat(draft["date"]), time.fromisoformat(draft["time"])
    status = local_status(day, moment, draft["tz"])
    if status == "nonexistent":
        await ask(update, context, state, step,
                  f"⚠️ {day:%d.%m.%Y} в {draft['tz']} времени {moment:%H:%M} нет — часы переводятся вперёд. "
                  + ("Выбери другую дату." if step == "date" else "Выбери другое время."))
        return
    if status == "ambiguous":
        state["origin"] = step
        await ask(update, context, state, "fold")
        return
    draft["fold"] = 0
    await after_moment(update, context, state, step)


async def after_moment(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, step: str) -> None:
    draft = state["draft"]
    anchor = to_utc(date.fromisoformat(draft["date"]), time.fromisoformat(draft["time"]), draft["tz"], draft["fold"])
    # New reminders start in the future; an edited repeating one may keep a past anchor.
    if anchor <= now() and (state["action"] == "create" or draft["repeat"] == "once"):
        await ask(update, context, state, step, "⚠️ Это время уже прошло. Укажи момент в будущем.")
        return
    if state["action"] == "create":
        await ask(update, context, state, "repeat")
    else:
        await save_edit(update, context, state, step)


async def after_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    if state["action"] == "create":
        await ask(update, context, state, "confirm")
    else:
        await save_edit(update, context, state, "repeat")


async def save_new(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    draft = state["draft"]
    schedule = draft_schedule(draft)
    first = next_run(schedule, now())
    if first is None:
        await ask(update, context, state, "time", "⚠️ Пока ты подтверждал, это время прошло. Укажи новое.")
        return
    reminder_id = await asyncio.to_thread(
        store.create_reminder, db(context), update.effective_user.id, draft["text"], schedule, first,
    )
    clear_reminder_state(context)
    reminder = await asyncio.to_thread(store.get_reminder, db(context), update.effective_user.id, reminder_id)
    sync_job(context.application, reminder, reminder_id)
    await respond(update, f"✅ Напоминание #{reminder_id} создано.\nПервое: {format_local(first, draft['tz'])} "
                          f"({draft['tz']}).\nПовторение: {describe(schedule)}.", InlineKeyboardMarkup(
        [[InlineKeyboardButton("📄 Открыть", callback_data=f"rem:open:{reminder_id}:list:0")]] + menu_rows(),
    ))


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict, step: str) -> None:
    draft, user_id = state["draft"], update.effective_user.id
    changes = {}
    if step == "text":
        changes["text"] = draft["text"]
    else:
        schedule = draft_schedule(draft)
        status = "paused" if state["status"] == "paused" else "active"
        upcoming = next_run(schedule, now())
        if upcoming is None and status == "active":
            await ask(update, context, state, step, "⚠️ Для напоминания без повторения это время уже прошло. "
                                                    "Сначала измени дату или время.")
            return
        changes = store.schedule_fields(schedule) | {"next_run_utc": store.to_db_time(upcoming), "status": status}
    saved = await asyncio.to_thread(
        store.update_reminder, db(context), user_id, state["id"], state["version"], **changes,
    )
    clear_reminder_state(context)
    if not saved:
        await show_reminder(update, context, state["id"], state["mode"], state["page"],
                            "⚠️ Напоминание изменилось или удалено, пока шло редактирование. Изменение не применено.")
        return
    reminder = await asyncio.to_thread(store.get_reminder, db(context), user_id, state["id"])
    sync_job(context.application, reminder, state["id"])
    await show_reminder(update, context, state["id"], state["mode"], state["page"], "✅ Напоминание обновлено.")


async def set_active(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reminder_id: int, mode: str, page: int, active: bool,
) -> None:
    user_id = update.effective_user.id
    reminder = await asyncio.to_thread(store.get_reminder, db(context), user_id, reminder_id)
    if reminder is None:
        await show_reminders(update, context, mode, page, "Напоминание не найдено — возможно, оно удалено.")
        return
    if active == (reminder.status == "active"):
        await show_reminder(update, context, reminder_id, mode, page,
                            "Напоминание уже включено." if active else "Напоминание уже отключено.")
        return
    if active:
        upcoming = next_run(reminder.schedule, now())
        if upcoming is None:
            await show_reminder(update, context, reminder_id, mode, page,
                                "⚠️ Время этого напоминания уже прошло. Измени дату или время — "
                                "после этого оно включится.")
            return
        changes = {"status": "active", "next_run_utc": store.to_db_time(upcoming)}
    else:
        changes = {"status": "paused"}
    saved = await asyncio.to_thread(
        store.update_reminder, db(context), user_id, reminder_id, reminder.version, **changes,
    )
    if saved:
        sync_job(context.application, await asyncio.to_thread(store.get_reminder, db(context), user_id, reminder_id),
                 reminder_id)
    notice = ("▶️ Напоминание включено." if active else "⏸ Напоминание отключено.") if saved else \
        "⚠️ Напоминание изменилось одновременно с этим действием. Проверь его состояние."
    await show_reminder(update, context, reminder_id, mode, page, notice)


async def confirm_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reminder_id: int, mode: str, page: int,
) -> None:
    reminder = await asyncio.to_thread(store.get_reminder, db(context), update.effective_user.id, reminder_id)
    if reminder is None:
        await show_reminders(update, context, mode, page, "Напоминание не найдено — возможно, оно удалено.")
        return
    state = new_state("delete", id=reminder_id, mode=mode, page=page)
    context.user_data[STATE_KEY] = state
    await respond(update, f"Удалить напоминание #{reminder.id}?\n\n{reminder.text}\n\n"
                          "Восстановить его будет нельзя. Отмена: /cancel.", InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"rem:confirm:{state['nonce']}")],
        [InlineKeyboardButton("Отмена", callback_data=f"rem:reject:{state['nonce']}")],
    ]))


# ---------- Commands ----------

def command_text(update: Update) -> str:
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) == 2 else ""


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_reminder_state(context)
    await begin_create(update, context, command_text(update))


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_reminder_state(context)
    if context.args:
        await respond(update, "Использование: /reminders. Страницы переключаются кнопками.")
        return
    await show_reminders(update, context)


async def reminder_action_command(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if not await private_chat(update):
        return
    clear_reminder_state(context)
    command = "editremind" if action == "edit" else "delremind"
    if not context.args:
        await show_reminders(update, context, f"{action}_list")
        return
    try:
        if len(context.args) != 1:
            raise ValueError
        reminder_id = parse_id(context.args[0])
    except ValueError:
        await respond(update, f"Использование: /{command} ID, например /{command} 3.")
        return
    if action == "edit":
        await show_reminder(update, context, reminder_id)
    else:
        await confirm_delete(update, context, reminder_id, "list", 0)


async def editremind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reminder_action_command(update, context, "edit")


async def delremind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reminder_action_command(update, context, "delete")


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_chat(update):
        return
    clear_reminder_state(context)
    zone = command_text(update)
    if zone:
        await save_timezone(update, context, zone)
    else:
        context.user_data[STATE_KEY] = {"action": "tz", "then": ""}
        await show_timezone_picker(update, context)


async def cancel_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle /cancel when a reminder dialog is active; False lets other sections handle it."""
    if STATE_KEY not in (context.user_data or {}):
        return False
    if await private_chat(update):
        await show_reminder_menu(update, context, "Действие отменено.")
    return True


async def consume_reminder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = (context.user_data or {}).get(STATE_KEY)
    if not state or state["action"] == "delete":
        return False
    if not await private_chat(update):
        return True
    text = update.effective_message.text or ""
    if state["action"] == "tz":
        await save_timezone(update, context, text, state.get("then", ""))
    elif state["step"] in ("text", "date", "time"):
        await accept(update, context, state, state["step"], text)
    else:
        await ask(update, context, state, state["step"], "Выбери вариант кнопкой ниже или нажми /cancel.")
    return True


# ---------- Callback buttons ----------

STEP_VALUES = {
    "date": r"\d{4}-\d{2}-\d{2}", "fold": r"[01]", "repeat": "|".join(REPEAT_TITLES),
    "day": r"[0-6]", "days": "ok", "confirm": "ok",
}
STEP_OF_VALUE = {"date": "date", "fold": "fold", "repeat": "repeat", "day": "days", "days": "days",
                 "confirm": "confirm"}


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await private_chat(update):
        return
    data = query.data if isinstance(query.data, str) else ""
    state = context.user_data.get(STATE_KEY) or {}
    stale = "Кнопка устарела. Открой /reminders."
    if data == "rem:menu":
        await show_reminder_menu(update, context)
    elif data == "rem:cancel":
        await show_reminder_menu(update, context, "Действие отменено.")
    elif data == "rem:add":
        clear_reminder_state(context)
        await begin_create(update, context)
    elif data == "rem:tz":
        context.user_data[STATE_KEY] = {"action": "tz", "then": ""}
        await show_timezone_picker(update, context)
    elif match := re.fullmatch(r"rem:tzinput(:c)?", data):
        context.user_data[STATE_KEY] = {"action": "tz", "then": "create" if match[1] else ""}
        await respond(update, "Введи IANA-название часового пояса, например Europe/Moscow. Отмена: /cancel.",
                      InlineKeyboardMarkup([cancel_row()]))
    elif match := re.fullmatch(r"rem:tzset(:c)?:([A-Za-z0-9_+\-/]{1,40})", data):
        await save_timezone(update, context, match[2], "create" if match[1] else "")
    elif match := re.fullmatch(r"rem:v:([0-9a-f]{8}):(\w+):([\w\-]+)", data):
        kind, value = match[2], match[3]
        if (state.get("nonce") != match[1] or kind not in STEP_VALUES or state.get("step") != STEP_OF_VALUE[kind]
                or not re.fullmatch(STEP_VALUES[kind], value)):
            await respond(update, "Эта кнопка относится к завершённому или другому диалогу. "
                                  "Продолжи текущий шаг или открой /reminders.")
            return
        await accept(update, context, state, kind, value)
    elif match := re.fullmatch(r"rem:(confirm|reject):([0-9a-f]{8})", data):
        if state.get("action") != "delete" or state.get("nonce") != match[2]:
            await respond(update, "Подтверждение устарело. Открой /reminders и выбери напоминание заново.",
                          reminder_menu())
            return
        clear_reminder_state(context)
        if match[1] == "reject":
            await show_reminders(update, context, state["mode"], state["page"], "Удаление отменено.")
            return
        deleted = await asyncio.to_thread(store.delete_reminder, db(context), update.effective_user.id, state["id"])
        if deleted:
            cancel_job(context.application, state["id"])
        await show_reminders(update, context, state["mode"], state["page"],
                             "🗑 Напоминание удалено." if deleted else "Напоминание не найдено.")
    elif match := re.fullmatch(r"rem" + BACK, data):
        clear_reminder_state(context)
        await show_reminders(update, context, match[1], int(match[2]))
    elif match := re.fullmatch(
        r"rem:(open|del|pause|resume|e_text|e_date|e_time|e_repeat):([0-9]{1,19})" + BACK, data,
    ):
        try:
            reminder_id = parse_id(match[2])
        except ValueError:
            await respond(update, stale, reminder_menu())
            return
        clear_reminder_state(context)
        action, mode, page = match[1], match[3], int(match[4])
        if action == "open":
            await show_reminder(update, context, reminder_id, mode, page)
        elif action == "del":
            await confirm_delete(update, context, reminder_id, mode, page)
        elif action in ("pause", "resume"):
            await set_active(update, context, reminder_id, mode, page, action == "resume")
        else:
            await begin_edit(update, context, reminder_id, action.removeprefix("e_"), mode, page)
    else:
        await respond(update, stale, reminder_menu())


def register_reminder_handlers(application) -> None:
    for command, callback in (
        ("remind", remind_command), ("reminders", reminders_command), ("editremind", editremind_command),
        ("delremind", delremind_command), ("timezone", timezone_command),
    ):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern=r"^rem:"))
