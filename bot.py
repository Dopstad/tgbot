import asyncio
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Говорим программе, что живём по времени Екатеринбурга, даже если сервер
# стоит в другой стране. Без этого слова вроде «завтра» могут считаться
# от времени сервера, а не твоего.
os.environ["TZ"] = "Asia/Yekaterinburg"
if hasattr(time, "tzset"):
    time.tzset()

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
# Шаблоны (регулярные выражения) — это «лупа», которой бот ищет в тексте
# нужные слова. Флаг re.IGNORECASE значит «регистр не важен».

FLAGS = re.IGNORECASE

# Дни недели во всех формах: «в пятницу», «пятница», «в воскресенье»
WEEKDAY_WORDS = r"(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресенье)"
WEEKDAY_STEMS = [
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
    ("пятниц", 4), ("суббот", 5), ("воскресень", 6),
]

# «завтра», «послезавтра», «сегодня»
RE_DAYWORD = re.compile(r"\b(послезавтра|завтра|сегодня)\b", FLAGS)

# «в пятницу», «в следующую пятницу», «пятница»
RE_WEEKDAY = re.compile(
    r"(?:\b(?:в|во)\s+)?"
    r"(?:(?P<mod>следующ\w+|эт\w+|ближайш\w+)\s+)?"
    r"\b(?P<wd>" + WEEKDAY_WORDS[1:-1] + r")\b",
    FLAGS,
)

# «на следующей неделе» / «на этой неделе»
RE_NEXT_WEEK = re.compile(r"\bна\s+следующ\w*\s+недел\w*", FLAGS)
RE_THIS_WEEK = re.compile(r"\bна\s+эт\w*\s+недел\w*", FLAGS)

# Время: «в 15:00», «в 7 вечера», «в 9 утра», «в 15 часов»
RE_TIME_COLON = re.compile(r"(?:\b(?:в|к)\s*)?\b(\d{1,2}):(\d{2})\b", FLAGS)
RE_TIME_PART = re.compile(r"\b(?:в|к)\s+(\d{1,2})\s*(утра|дня|вечера|ночи)\b", FLAGS)
RE_TIME_HOURS = re.compile(r"\b(?:в|к)\s+(\d{1,2})\s*(?:часов|часа|час|ч)\b", FLAGS)


def now_local():
    """Текущее время в Екатеринбурге."""
    return datetime.now(TZ)


def weekday_index(word):
    """Превращает слово «пятницу» в номер дня недели (пн=0 ... вс=6)."""
    word = word.lower()
    for stem, index in WEEKDAY_STEMS:
        if word.startswith(stem):
            return index
    return None


def cut(text, match):
    """Вырезает найденный кусок из текста."""
    return text[: match.start()] + " " + text[match.end():]


def clean_task(text):
    """Оставляет чистую суть задачи: убирает «напомни мне» и лишние знаки."""
    text = re.sub(r"^\s*(напомни|напиши)\s+(мне\s+)?", "", text, flags=FLAGS)
    text = re.sub(r"\s+", " ", text).strip(" ,.-—:")
    # Если в конце остался повисший предлог («Позвонить Ивану в»), убираем его
    text = re.sub(r"\s+(в|во|на|к)$", "", text, flags=FLAGS)
    return text.strip(" ,.-—:")


def parse_with_dateparser(text):
    """
    Запасной вариант: библиотека dateparser. Она умеет «через 2 часа»,
    «15.11 в 10:00» и подобное. Работает, если свои правила ничего не нашли.
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
    task = clean_task(text.replace(fragment, ""))

    has_clock = re.search(r"\d{1,2}[:.]\d{2}", fragment)
    has_relative = re.search(r"через", fragment, flags=FLAGS)
    has_hours = re.search(r"\bв\s+\d{1,2}\s*(час|ч\b)", fragment, flags=FLAGS)

    if not (has_clock or has_relative or has_hours):
        return task, None
    return task, when


def parse_task(text):
    """
    Разбирает фразу на суть задачи и время напоминания.
    Возвращает:
      None            - если дату в тексте найти не удалось
      (задача, None)  - если день есть, а времени нет
      (задача, время) - если всё нашлось
    """
    now = now_local()
    work = text

    # ---- 1. Ищем время ----
    hour = minute = None
    m = RE_TIME_COLON.search(work)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = RE_TIME_PART.search(work)
        if m:
            h, part = int(m.group(1)), m.group(2).lower()
            minute = 0
            if part in ("утра", "ночи"):
                hour = 0 if h == 12 else h
            elif part == "дня":
                hour = h if h >= 12 else h + 12
            else:  # вечера
                hour = h + 12 if h < 12 else h
        else:
            m = RE_TIME_HOURS.search(work)
            if m:
                hour, minute = int(m.group(1)), 0
    if m:
        work = cut(work, m)
    if hour is not None and not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour = minute = None

    # ---- 2. Ищем день ----
    next_week = bool(RE_NEXT_WEEK.search(work))
    this_week = bool(RE_THIS_WEEK.search(work))
    work = RE_NEXT_WEEK.sub(" ", work)
    work = RE_THIS_WEEK.sub(" ", work)

    day = None
    auto_weekday = False  # True, если день недели назван без «следующей/этой»

    m = RE_DAYWORD.search(work)
    if m:
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1).lower()]
        day = now.date() + timedelta(days=offset)
        work = cut(work, m)
    else:
        m = RE_WEEKDAY.search(work)
        if m:
            index = weekday_index(m.group("wd"))
            mod = (m.group("mod") or "").lower()
            today = now.date()
            monday = today - timedelta(days=today.weekday())  # понедельник этой недели
            if next_week or mod.startswith("следующ"):
                day = monday + timedelta(days=7 + index)
            elif this_week or mod.startswith("эт"):
                day = monday + timedelta(days=index)
            else:
                day = today + timedelta(days=(index - today.weekday()) % 7)
                auto_weekday = True
            work = cut(work, m)

    # Своими правилами день не нашёлся — пробуем запасной вариант
    if day is None:
        return parse_with_dateparser(text)

    task = clean_task(work)
    if hour is None:
        return task, None

    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)

    # «В пятницу» в саму пятницу, когда время уже прошло, значит следующая пятница
    if auto_weekday and when <= now:
        when += timedelta(days=7)

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
        "Позвонить Ивану завтра в 15:00\n"
        "Тренировка в пятницу в 19:00\n"
        "Планёрка в понедельник на следующей неделе в 9:00\n\n"
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

    if when < now_local():
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
