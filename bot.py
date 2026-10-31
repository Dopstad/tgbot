import asyncio
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from dateparser.search import search_dates
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Твой часовой пояс: Екатеринбург (UTC+5)
TZ = ZoneInfo("Asia/Yekaterinburg")

# Имя файла, где будут храниться задачи (создастся сам рядом с bot.py)
DB_PATH = "tasks.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()


# ======================= БАЗА ДАННЫХ =======================
# База — это таблица, как в Excel. Каждая строка — одна задача:
#   id        — номер задачи (выдаётся автоматически)
#   chat_id   — чей это чат (чтобы у каждого человека были свои задачи)
#   text      — суть задачи
#   remind_at — когда напомнить (число-метка времени)
#   sent      — 0 = напоминание ещё не отправлено, 1 = уже отправлено

def run_db(query, params=(), fetch=False):
    """Открывает базу, выполняет один запрос и закрывает её."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows if fetch else cur.rowcount
    finally:
        conn.close()


def init_db():
    """Создаёт таблицу при первом запуске. Если она уже есть — ничего не трогает."""
    run_db(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def add_task(chat_id, text, when):
    """Сохраняет новую задачу. Время хранится как число (timestamp)."""
    run_db(
        "INSERT INTO tasks (chat_id, text, remind_at) VALUES (?, ?, ?)",
        (chat_id, text, int(when.timestamp())),
    )


def get_tasks(chat_id):
    """Возвращает все ещё не сработавшие задачи этого чата по порядку времени."""
    return run_db(
        "SELECT id, text, remind_at FROM tasks "
        "WHERE chat_id = ? AND sent = 0 ORDER BY remind_at",
        (chat_id,),
        fetch=True,
    )


def delete_task(chat_id, task_id):
    """Удаляет задачу. Возвращает 1, если удалили, и 0, если такой нет."""
    return run_db(
        "DELETE FROM tasks WHERE id = ? AND chat_id = ? AND sent = 0",
        (task_id, chat_id),
    )


def get_due_tasks(now_ts):
    """Возвращает задачи, время которых уже наступило, а напоминание не отправлено."""
    return run_db(
        "SELECT id, chat_id, text FROM tasks WHERE sent = 0 AND remind_at <= ?",
        (now_ts,),
        fetch=True,
    )


def mark_sent(task_id):
    """Помечает задачу как отправленную, чтобы она не пришла второй раз."""
    run_db("UPDATE tasks SET sent = 1 WHERE id = ?", (task_id,))


# ======================= РАЗБОР ТЕКСТА =======================

def parse_task(text):
    """
    Разбирает фразу на суть задачи и время напоминания.
    Возвращает:
      None            - если дату в тексте найти не удалось
      (задача, None)  - если дата есть, а времени нет
      (задача, время) - если всё нашлось
    """
    settings = {
        "TIMEZONE": "Asia/Yekaterinburg",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }

    found = search_dates(text, languages=["ru"], settings=settings)
    if not found:
        return None

    fragment, when = max(found, key=lambda item: len(item[0]))

    task = text.replace(fragment, "")
    task = re.sub(r"^\s*(напомни|напиши)\s+(мне\s+)?", "", task, flags=re.IGNORECASE)
    task = re.sub(r"\s+", " ", task).strip(" ,.-—:")

    has_clock = re.search(r"\d{1,2}[:.]\d{2}", fragment)
    has_relative = re.search(r"через", fragment, flags=re.IGNORECASE)
    has_hours = re.search(r"\bв\s+\d{1,2}\s*(час|ч\b)", fragment, flags=re.IGNORECASE)

    if not (has_clock or has_relative or has_hours):
        return task, None

    return task, when


# ======================= НАПОМИНАНИЯ =======================

async def reminder_loop():
    """
    Работает в фоне всё время, пока запущен бот.
    Каждые 20 секунд заглядывает в базу: не наступило ли время какой-то задачи?
    Если да — отправляет сообщение и помечает задачу как выполненную.
    """
    while True:
        try:
            now_ts = int(datetime.now(TZ).timestamp())
            for task_id, chat_id, text in get_due_tasks(now_ts):
                await bot.send_message(chat_id, f"⏰ Напоминание:\n{text}")
                mark_sent(task_id)
        except Exception as error:
            # Если что-то пошло не так (например, нет интернета), бот не падает,
            # а просто пишет ошибку в терминал и пробует снова через 20 секунд
            print("Ошибка в цикле напоминаний:", error)
        await asyncio.sleep(20)


# ======================= КОМАНДЫ БОТА =======================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я бот-напоминалка.\n\n"
        "Чтобы поставить задачу, напиши, например:\n"
        "Позвонить Ивану завтра в 15:00\n\n"
        "Команды:\n"
        "/list — все мои задачи\n"
        "/del 3 — удалить задачу №3 (или напиши «удали 3»)"
    )


@dp.message(Command("list"))
async def show_list(message: Message):
    tasks = get_tasks(message.chat.id)
    if not tasks:
        await message.answer("Активных задач нет.")
        return

    lines = ["Твои задачи:\n"]
    for task_id, text, remind_at in tasks:
        when = datetime.fromtimestamp(remind_at, TZ)
        lines.append(f"№{task_id} — {when.strftime('%d.%m.%Y %H:%M')} — {text}")
    lines.append("\nУдалить: /del номер")
    await message.answer("\n".join(lines))


async def do_delete(message: Message, raw_number):
    """Общая логика удаления, её используют команда /del и фраза «удали 3»."""
    if not raw_number or not raw_number.strip().isdigit():
        await message.answer("Укажи номер задачи, например: /del 3\nНомера видно в /list")
        return

    if delete_task(message.chat.id, int(raw_number)):
        await message.answer(f"Задача №{raw_number.strip()} удалена 🗑")
    else:
        await message.answer("Задачи с таким номером нет. Посмотри номера в /list")


@dp.message(Command("del"))
async def delete_by_command(message: Message, command: CommandObject):
    await do_delete(message, command.args)


# Фраза вида «удали 3» / «удалить 3» / «удали задачу 3»
@dp.message(F.text.regexp(r"(?i)^\s*удал\w*\s+(задач\w*\s+)?(№\s*)?(\d+)\s*$"))
async def delete_by_text(message: Message):
    number = re.search(r"(\d+)\s*$", message.text).group(1)
    await do_delete(message, number)


# Любой другой текст — считаем, что это новая задача
@dp.message(F.text)
async def handle_task(message: Message):
    result = parse_task(message.text)

    if result is None:
        await message.answer(
            "Не нашёл в сообщении дату. Попробуй так:\n"
            "Позвонить Ивану завтра в 15:00"
        )
        return

    task, when = result

    if when is None:
        await message.answer("Укажи время в формате 15:00, например: завтра в 15:00")
        return

    if when < datetime.now(TZ):
        await message.answer("Это время уже прошло. Укажи время в будущем.")
        return

    if not task:
        task = "Напоминание"

    add_task(message.chat.id, task, when)

    await message.answer(
        f"Записал ✅\n"
        f"Задача: {task}\n"
        f"Напомню: {when.strftime('%d.%m.%Y в %H:%M')} (Екатеринбург)"
    )


# ======================= ЗАПУСК =======================

async def main():
    init_db()  # создаём базу, если её ещё нет
    # Запускаем цикл напоминаний в фоне, параллельно с приёмом сообщений
    reminder_task = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())