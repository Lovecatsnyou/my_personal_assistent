"""Application entry point and main menu; feature modules register their handlers."""
import asyncio
import fcntl
import logging
import os
import sqlite3

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, TypeHandler, filters,
)

from database import DB_PATH, Database
from note_handlers import (
    NOTE_COMMANDS, NOTES_BUTTON, cancel_note_action, clear_note_state, consume_note_text,
    register_note_handlers, show_note_menu,
)
from reminder_handlers import (
    REMINDER_COMMANDS, REMINDERS_BUTTON, cancel_reminder_action, clear_reminder_state,
    consume_reminder_text, register_reminder_handlers, show_reminder_menu,
)
from reminder_scheduler import restore_on_start
from task_handlers import (
    TASK_COMMANDS, TASKS_BUTTON, cancel_command as cancel_task_action, clear_task_state,
    consume_task_text, get_database, register_task_handlers, respond, show_task_menu,
)
from weather_handlers import (
    WEATHER_BUTTON, WEATHER_COMMANDS, cancel_weather_action, clear_weather_state, close_weather,
    consume_weather_text, register_weather_handlers, show_weather_menu,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# HTTP request URLs may contain the token. Do not log successful requests.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MENU = ReplyKeyboardMarkup(
    [["📋 Задачи", "📝 Заметки"], ["⏰ Напоминания", "🌤 Погода"], ["📰 Новости", "ℹ️ Помощь"]],
    resize_keyboard=True,
)

# Section name (callback prefix) -> function that drops its unfinished dialog.
SECTION_STATES = {
    "tasks": clear_task_state, "notes": clear_note_state, "rem": clear_reminder_state, "wx": clear_weather_state,
}


def clear_states(context: ContextTypes.DEFAULT_TYPE) -> None:
    for clear in SECTION_STATES.values():
        clear(context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_states(context)
    name = update.effective_user.first_name if update.effective_user else "друг"
    await update.effective_message.reply_text(
        f"Привет, {name}! 👋\nЯ твой персональный ассистент. Выбери раздел в меню.",
        reply_markup=MENU,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_states(context)
    await update.effective_message.reply_text(
        "Команды:\n/start — открыть главное меню\n/help — помощь\n\n"
        "Задачи:\n/add [название] — создать задачу\n/tasks — мои задачи\n"
        "/done [ID] — выполнить задачу\n/delete [ID] — удалить с подтверждением\n\n"
        "Заметки:\n/note [заголовок] — создать заметку\n/notes — мои заметки\n"
        "/searchnotes [текст] — поиск по заголовкам и тексту\n"
        "/editnote [ID] — изменить заголовок или текст\n/deletenote [ID] — удалить с подтверждением\n\n"
        "Напоминания:\n/remind [текст] — создать напоминание\n/reminders — мои напоминания\n"
        "/editremind [ID] — изменить, отключить или включить\n/delremind [ID] — удалить с подтверждением\n"
        "/timezone [пояс] — часовой пояс, например /timezone Europe/Moscow\n\n"
        "Погода (данные Open-Meteo):\n/weather [город] — текущая погода\n"
        "/forecast [город] — прогноз на 7 дней\n/setcity [город] — выбрать основной город\n"
        "/mycity — мой город\n\n"
        "/cancel — отменить ввод или удаление\n\n"
        "Кнопки «📋 Задачи», «📝 Заметки», «⏰ Напоминания» и «🌤 Погода» открывают интерактивные меню. "
        "В списках по 5 записей на страницу. Эти разделы доступны в личном чате с ботом. "
        "Раздел новостей пока является заготовкой.",
        reply_markup=MENU,
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or ""
    sections = {
        "📰 Новости": "📰 Новости добавим позже.",
    }
    if text == TASKS_BUTTON:
        clear_states(context)
        await show_task_menu(update, context)
    elif text == NOTES_BUTTON:
        clear_states(context)
        await show_note_menu(update, context)
    elif text == REMINDERS_BUTTON:
        clear_states(context)
        await show_reminder_menu(update, context)
    elif text == WEATHER_BUTTON:
        clear_states(context)
        await show_weather_menu(update, context)
    elif text == "ℹ️ Помощь":
        await help_command(update, context)
    elif text in sections:
        clear_states(context)
        await message.reply_text(sections[text], reply_markup=MENU)
    elif (await consume_task_text(update, context) or await consume_note_text(update, context)
          or await consume_reminder_text(update, context) or await consume_weather_text(update, context)):
        return
    else:
        await message.reply_text("Выбери раздел в меню или нажми /help.", reply_markup=MENU)


async def register_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_chat and update.effective_chat.type == "private":
        await asyncio.to_thread(
            get_database(context).upsert_user, user.id, user.first_name, user.username, user.last_name,
        )


async def switch_section(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An action in one section abandons unfinished input or confirmation in the other."""
    if context.user_data is None:
        return
    section = None
    if update.callback_query and isinstance(update.callback_query.data, str):
        section = update.callback_query.data.split(":", 1)[0]
    elif update.effective_message and (update.effective_message.text or "").startswith("/"):
        command = update.effective_message.text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        section = next((name for name, commands in (
            ("tasks", TASK_COMMANDS), ("notes", NOTE_COMMANDS), ("rem", REMINDER_COMMANDS),
            ("wx", WEATHER_COMMANDS),
        ) if command in commands), None)
    if section in SECTION_STATES:
        for name, clear in SECTION_STATES.items():
            if name != section:
                clear(context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (await cancel_note_action(update, context) or await cancel_reminder_action(update, context)
            or await cancel_weather_action(update, context)):
        await cancel_task_action(update, context)


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    clear_states(context)
    await respond(update, "Возврат в главное меню.")
    await start(update, context)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Неизвестная команда. Список команд: /help. Отмена: /cancel.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Exception messages/updates may include a token or private task text.
    logger.error("Ошибка обработки обновления: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        text = (
            "База данных временно недоступна. Повтори действие позже."
            if isinstance(context.error, sqlite3.Error)
            else "Не удалось обработать действие. Попробуй ещё раз или нажми /cancel."
        )
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            logger.warning("Не удалось отправить сообщение об ошибке")
    # In particular, do not run feature handlers after registration has failed.
    raise ApplicationHandlerStop


def build_application(token: str, database: Database | None = None) -> Application:
    database = database if database is not None else Database()
    database.initialize()
    # Sequential updates keep per-user input and deletion confirmations consistent.
    app = (Application.builder().token(token).concurrent_updates(False)
           .post_init(restore_on_start).post_shutdown(close_weather).build())
    if app.job_queue is None:
        raise RuntimeError('Нет JobQueue: установите зависимости, pip install -r requirements.txt')
    app.bot_data["database"] = database
    app.add_handler(TypeHandler(Update, register_user), group=-2)
    app.add_handler(TypeHandler(Update, switch_section), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(home_callback, pattern=r"^home$"))
    register_task_handlers(app)
    register_note_handlers(app)
    register_reminder_handlers(app)
    register_weather_handlers(app)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(error_handler)
    return app

def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Смотри README.md")
    # A second copy would send every reminder twice and conflict on getUpdates.
    lock = open(DB_PATH.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Бот уже запущен в другом процессе. Остановите его перед новым запуском.") from None
    app = build_application(token)
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
