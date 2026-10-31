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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dateparser.search import search_dates
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Твой часовой пояс: Екатеринбург (UTC+5)
TZ = ZoneInfo("Asia/Yekaterinburg")

# Имя файла, где будут храниться данные (создастся сам рядом с bot.py)
DB_PATH = "tasks.db"

# Во сколько присылать утреннюю сводку по умолчанию
DEFAULT_DIGEST_TIME = "08:00"

bot = Bot(token=TOKEN)
dp = Dispatcher()


# ======================= БАЗА ДАННЫХ =======================
# База — это набор таблиц, как в Excel.
#
# Таблица tasks — задачи:
#   id          — номер задачи (выдаётся автоматически)
#   chat_id     — чей это чат
#   text        — суть задачи
#   remind_at   — когда напомнить (число-метка времени)
#   sent        — 0 = ещё не сработала, 1 = уже сработала
#   repeat_rule — правило повтора: пусто, "daily" или "wd:0,2,4" (дни недели, пн=0)
#   advance_min — за сколько минут предупреждать заранее: "30,60,2880"
#
# Таблица advances — запланированные предупреждения «заранее»
# Таблица users    — настройки утренней сводки для каждого чата

def run_db(query, params=(), fetch=False, want_id=False):
    """Открывает базу, выполняет один запрос и закрывает её."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        if want_id:
            return cur.lastrowid
        return rows if fetch else cur.rowcount
    finally:
        conn.close()


def init_db():
    """Создаёт таблицы при первом запуске и дополняет старую базу новыми колонками."""
    run_db(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            repeat_rule TEXT NOT NULL DEFAULT '',
            advance_min TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Если база осталась от старой версии бота, добавляем недостающие колонки.
    # Старые задачи при этом сохраняются.
    columns = [row[1] for row in run_db("PRAGMA table_info(tasks)", fetch=True)]
    if "repeat_rule" not in columns:
        run_db("ALTER TABLE tasks ADD COLUMN repeat_rule TEXT NOT NULL DEFAULT ''")
    if "advance_min" not in columns:
        run_db("ALTER TABLE tasks ADD COLUMN advance_min TEXT NOT NULL DEFAULT ''")

    run_db(
        """
        CREATE TABLE IF NOT EXISTS advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            fire_at INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    run_db(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            digest_on INTEGER NOT NULL DEFAULT 1,
            digest_time TEXT NOT NULL DEFAULT '08:00',
            last_digest TEXT NOT NULL DEFAULT ''
        )
        """
    )


def plan_advances(task_id, when, minutes_list, now=None):
    """
    Планирует предупреждения «заранее» для задачи. Старые предупреждения этой
    задачи стираются. Если время предупреждения уже прошло, оно пропускается.
    Возвращает (созданные, пропущенные).
    """
    now = now or now_local()
    run_db("DELETE FROM advances WHERE task_id = ?", (task_id,))
    made, skipped = [], []
    for minutes in sorted(set(minutes_list), reverse=True):
        fire_at = int(when.timestamp()) - minutes * 60
        if fire_at > int(now.timestamp()):
            run_db(
                "INSERT INTO advances (task_id, minutes, fire_at) VALUES (?, ?, ?)",
                (task_id, minutes, fire_at),
            )
            made.append(minutes)
        else:
            skipped.append(minutes)
    return made, skipped


def add_task(chat_id, text, when, repeat_rule="", advance=()):
    """Сохраняет новую задачу и планирует предупреждения. Возвращает (номер, созданные, пропущенные)."""
    advance_text = ",".join(str(m) for m in sorted(set(advance), reverse=True))
    task_id = run_db(
        "INSERT INTO tasks (chat_id, text, remind_at, repeat_rule, advance_min) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, text, int(when.timestamp()), repeat_rule, advance_text),
        want_id=True,
    )
    made, skipped = plan_advances(task_id, when, advance)
    return task_id, made, skipped


def get_tasks(chat_id):
    """Возвращает все ещё не сработавшие задачи этого чата по порядку времени."""
    return run_db(
        "SELECT id, text, remind_at, repeat_rule, advance_min FROM tasks "
        "WHERE chat_id = ? AND sent = 0 ORDER BY remind_at",
        (chat_id,),
        fetch=True,
    )


def get_tasks_for_day(chat_id, day):
    """Задачи на конкретный день (day — дата)."""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    return run_db(
        "SELECT id, text, remind_at, repeat_rule, advance_min FROM tasks "
        "WHERE chat_id = ? AND sent = 0 AND remind_at >= ? AND remind_at < ? "
        "ORDER BY remind_at",
        (chat_id, int(start.timestamp()), int(end.timestamp())),
        fetch=True,
    )


def delete_task(chat_id, task_id):
    """Удаляет задачу и её предупреждения. Возвращает 1, если удалили, и 0, если такой нет."""
    deleted = run_db(
        "DELETE FROM tasks WHERE id = ? AND chat_id = ? AND sent = 0",
        (task_id, chat_id),
    )
    if deleted:
        run_db("DELETE FROM advances WHERE task_id = ?", (task_id,))
    return deleted


def get_task_info(chat_id, task_id):
    """Достаёт текст и правило повтора задачи. Если такой задачи нет, вернёт None."""
    rows = run_db(
        "SELECT text, repeat_rule FROM tasks WHERE id = ? AND chat_id = ?",
        (task_id, chat_id),
        fetch=True,
    )
    return rows[0] if rows else None


def snooze_task(chat_id, task_id, minutes):
    """
    Откладывает задачу на N минут.
    Обычную задачу просто переносим. Повторяющуюся не трогаем (иначе сломается
    расписание), а создаём рядом разовую копию.
    """
    info = get_task_info(chat_id, task_id)
    if info is None:
        return False
    text, repeat_rule = info
    new_time = now_local() + timedelta(minutes=minutes)
    if repeat_rule:
        add_task(chat_id, text, new_time)
    else:
        run_db(
            "UPDATE tasks SET remind_at = ?, sent = 0 WHERE id = ? AND chat_id = ?",
            (int(new_time.timestamp()), task_id, chat_id),
        )
        run_db("DELETE FROM advances WHERE task_id = ?", (task_id,))
    return True


def ensure_user(chat_id):
    """
    Запоминает чат для утренней сводки. Если человек появился уже после
    времени сводки, на сегодня её не присылаем, начнём со следующего утра.
    """
    exists = run_db("SELECT 1 FROM users WHERE chat_id = ?", (chat_id,), fetch=True)
    if exists:
        return
    now = now_local()
    last = now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= DEFAULT_DIGEST_TIME else ""
    run_db(
        "INSERT OR IGNORE INTO users (chat_id, last_digest) VALUES (?, ?)",
        (chat_id, last),
    )


def set_digest(chat_id, on, hhmm=None):
    """Включает/выключает сводку и меняет время."""
    ensure_user(chat_id)
    if hhmm is None:
        hhmm = run_db("SELECT digest_time FROM users WHERE chat_id = ?", (chat_id,), fetch=True)[0][0]
    now = now_local()
    last = now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= hhmm else ""
    run_db(
        "UPDATE users SET digest_on = ?, digest_time = ?, last_digest = ? WHERE chat_id = ?",
        (1 if on else 0, hhmm, last, chat_id),
    )


# ======================= ПОВТОРЫ И ТЕКСТЫ =======================

WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def plural(n, one, few, many):
    """Правильное окончание: 1 час, 2 часа, 5 часов."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def fmt_before(minutes):
    """30 -> «за 30 минут», 120 -> «за 2 часа», 2880 -> «за 2 дня»."""
    if minutes % 10080 == 0:
        n = minutes // 10080
        return f"за {n} {plural(n, 'неделю', 'недели', 'недель')}"
    if minutes % 1440 == 0:
        n = minutes // 1440
        return f"за {n} {plural(n, 'день', 'дня', 'дней')}"
    if minutes % 60 == 0:
        n = minutes // 60
        return f"за {n} {plural(n, 'час', 'часа', 'часов')}"
    return f"за {minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"


def rule_days(rule):
    """Из правила повтора получает набор дней недели (пн=0 ... вс=6)."""
    if rule.startswith("wd:"):
        return {int(x) for x in rule[3:].split(",") if x != ""}
    return set(range(7))


def describe_repeat(rule):
    """Правило повтора словами."""
    days = rule_days(rule)
    if rule == "daily" or days == set(range(7)):
        return "каждый день"
    if days == {0, 1, 2, 3, 4}:
        return "по будням"
    if days == {5, 6}:
        return "по выходным"
    return "по " + ", ".join(WD_SHORT[d] for d in sorted(days))


def next_occurrence(rule, hour, minute, after):
    """Ближайшее время по правилу повтора, которое строго позже момента after."""
    days = rule_days(rule)
    for add in range(0, 9):
        d = after.date() + timedelta(days=add)
        candidate = datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)
        if candidate > after and candidate.weekday() in days:
            return candidate
    return None


def short_when(dt):
    """«пн 02.11 в 09:00»."""
    return f"{WD_SHORT[dt.weekday()]} {dt.strftime('%d.%m')} в {dt.strftime('%H:%M')}"


def task_line(task_id, text, remind_at, repeat_rule, advance_min):
    """Одна строка для списка задач."""
    when = datetime.fromtimestamp(remind_at, TZ)
    line = f"№{task_id} — {when.strftime('%d.%m.%Y %H:%M')} — {text}"
    extras = []
    if repeat_rule:
        extras.append("🔁 " + describe_repeat(repeat_rule))
    if advance_min:
        extras.append("🔔 " + ", ".join(fmt_before(int(x)) for x in advance_min.split(",")))
    if extras:
        line += "\n      " + " · ".join(extras)
    return line


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

# Любая форма дня недели, в том числе «понедельникам», «средам»
WD_ANY = (
    r"(?:понедельник\w*|вторник\w*|сред[ауы]\w*|четверг\w*|"
    r"пятниц\w*|суббот\w*|воскресень\w*)"
)

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

# Повторы: «каждый день», «по будням», «по выходным»,
# «каждый понедельник», «по понедельникам, средам и пятницам»
RE_EVERY_DAY = re.compile(r"\b(?:кажд\w+\s+(?:день|сутки)|ежедневно)\b", FLAGS)
RE_WORKDAYS = re.compile(
    r"\b(?:по\s+будн\w+|по\s+рабочим\s+дням|кажд\w+\s+будн\w+\s+день)\b", FLAGS
)
RE_WEEKENDS = re.compile(r"\b(?:по\s+выходным|кажд\w+\s+выходн\w+)\b", FLAGS)
RE_REPEAT_DAYS = re.compile(
    r"\b(?:кажд\w+|по)\s+(" + WD_ANY + r"(?:\s*(?:,\s*и|,|и)\s*" + WD_ANY + r")*)\b",
    FLAGS,
)

# «Заранее»: «за 30 минут», «за 1 час», «за 2 дня», «за неделю», «за полчаса»
RE_ADVANCE = re.compile(
    r"\bза\s+(?:(\d+)\s*)?"
    r"(минуту|минуты|минут|мин|часа|часов|час|ч|дня|дней|день|дн|суток|сутки|"
    r"недели|неделю|недель|неделя)\b",
    FLAGS,
)
RE_HALF_HOUR = re.compile(r"\bза\s+полчаса\b", FLAGS)
RE_ZARANEE = re.compile(r"\bзаранее\b", FLAGS)


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
    """Оставляет чистую суть задачи: убирает «напомни мне», лишние знаки и повисшие слова."""
    text = re.sub(r"^\s*(напомни|напиши|предупреди)\s+(мне\s+)?", "", text, flags=FLAGS)
    text = re.sub(r"\s+", " ", text).strip(" ,.-—:")
    # Убираем повисшие в конце слова: «Позвонить Ивану в», «Созвон и», «Встреча, напомни»
    for _ in range(4):
        text = re.sub(
            r"[\s,]+(в|во|на|к|и|до|за|напомни|предупреди|напиши)$", "", text, flags=FLAGS
        ).strip(" ,.-—:")
    return text


def make_result(task, when, repeat="", advance=(), advance_missing=False):
    """Результат разбора: всё, что бот понял из сообщения."""
    return {
        "task": task,
        "when": when,
        "repeat": repeat,
        "advance": list(advance),
        "advance_missing": advance_missing,
    }


def parse_with_dateparser(text):
    """
    Запасной вариант: библиотека dateparser. Она умеет «через 2 часа»,
    «15.11 в 10:00» и подобное. Работает, если свои правила ничего не нашли.
    Возвращает None, (задача, None) или (задача, время).
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
    Разбирает фразу: суть задачи, время, повтор и предупреждения «заранее».
    Возвращает None, если ничего про время найти не удалось,
    иначе словарь (см. make_result).
    """
    now = now_local()
    base = text

    # ---- 1. Повтор: «каждый день», «по понедельникам» ----
    repeat = ""
    m = RE_EVERY_DAY.search(base)
    if m:
        repeat = "daily"
    else:
        m = RE_WORKDAYS.search(base)
        if m:
            repeat = "wd:0,1,2,3,4"
        else:
            m = RE_WEEKENDS.search(base)
            if m:
                repeat = "wd:5,6"
            else:
                m = RE_REPEAT_DAYS.search(base)
                if m:
                    days = sorted({weekday_index(w) for w in re.findall(WD_ANY, m.group(1), FLAGS)})
                    repeat = "wd:" + ",".join(str(d) for d in days)
    if repeat:
        base = cut(base, m)

    # ---- 2. «Заранее»: «за 30 минут», «за 2 дня» ----
    advance = []

    def grab(match):
        count = int(match.group(1)) if match.group(1) else 1
        unit = match.group(2).lower()
        if unit.startswith("мин"):
            minutes = count
        elif unit.startswith("ч"):
            minutes = count * 60
        elif unit.startswith(("д", "сут")):
            minutes = count * 1440
        else:  # недели
            minutes = count * 10080
        if minutes > 0:
            advance.append(minutes)
        return " "

    base = RE_ADVANCE.sub(grab, base)
    base = RE_HALF_HOUR.sub(lambda match: (advance.append(30), " ")[1], base)
    said_zaranee = bool(RE_ZARANEE.search(base))
    base = RE_ZARANEE.sub(" ", base)
    advance = sorted(set(advance), reverse=True)
    advance_missing = said_zaranee and not advance

    work = base

    # ---- 3. Время ----
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

    # ---- 4. Повторяющаяся задача: день не нужен, считаем первое срабатывание ----
    if repeat:
        task = clean_task(work)
        if hour is None:
            return make_result(task, None, repeat, advance, advance_missing)
        first = next_occurrence(repeat, hour, minute, now)
        return make_result(task, first, repeat, advance, advance_missing)

    # ---- 5. День: «завтра», «в пятницу», «на следующей неделе» ----
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
        fallback = parse_with_dateparser(base)
        if fallback is None:
            return None
        task, when = fallback
        return make_result(task, when, "", advance, advance_missing)

    task = clean_task(work)
    if hour is None:
        return make_result(task, None, "", advance, advance_missing)

    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)

    # «В пятницу» в саму пятницу, когда время уже прошло, значит следующая пятница
    if auto_weekday and when <= now:
        when += timedelta(days=7)

    return make_result(task, when, "", advance, advance_missing)


# ======================= МЕНЮ И ТЕКСТЫ =======================

BTN_LIST = "📋 Мои задачи"
BTN_TODAY = "📅 На сегодня"
BTN_HELP = "ℹ️ Как пользоваться"

HELP_TEXT = (
    "Я бот-напоминалка. Просто напиши, что и когда напомнить:\n\n"
    "• Позвонить Ивану завтра в 15:00\n"
    "• Тренировка в пятницу в 19:00\n"
    "• Планёрка в понедельник на следующей неделе в 9:00\n"
    "• Купить воду через 2 часа\n\n"
    "Заранее (можно несколько сразу):\n"
    "• Созвон завтра в 15:00, напомни за 30 минут\n"
    "• Конференция в пятницу в 10:00 за 1 час и за 2 дня\n"
    "Можно за минуты, часы, дни и недели.\n\n"
    "Повторы:\n"
    "• Зарядка каждый день в 7:00\n"
    "• Планёрка каждый понедельник в 9:00\n"
    "• Тренировка по понедельникам, средам и пятницам в 19:00\n"
    "• Отчёт по будням в 17:30\n\n"
    "Команды:\n"
    "/list — все задачи (с кнопками удаления)\n"
    "/today — задачи на сегодня\n"
    "/del 3 — удалить задачу №3 (или напиши «удали 3»)\n"
    "/digest — утренняя сводка: /digest 7:30, /digest off"
)


def main_menu():
    """Кнопки внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_TODAY)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def render_list(chat_id):
    """Текст списка задач и кнопки удаления под ним."""
    tasks = get_tasks(chat_id)
    if not tasks:
        return "Активных задач нет.", None

    shown = tasks[:25]
    lines = ["Твои задачи:\n"]
    lines += [task_line(*task) for task in shown]
    if len(tasks) > len(shown):
        lines.append(f"\n...и ещё {len(tasks) - len(shown)}")
    lines.append("\nУдалить: нажми кнопку с номером или напиши /del номер")

    buttons = [
        InlineKeyboardButton(text=f"🗑 №{task[0]}", callback_data=f"del:{task[0]}")
        for task in shown
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_today(chat_id, now):
    """Задачи на сегодня. Если их нет, вернёт None."""
    tasks = get_tasks_for_day(chat_id, now.date())
    if not tasks:
        return None
    lines = []
    for _, text, remind_at, repeat_rule, _adv in tasks:
        when = datetime.fromtimestamp(remind_at, TZ)
        mark = " 🔁" if repeat_rule else ""
        lines.append(f"{when.strftime('%H:%M')} — {text}{mark}")
    return "\n".join(lines)


# ======================= НАПОМИНАНИЯ =======================

def reminder_keyboard(task_id):
    """
    Кнопки под напоминанием. В callback_data мы прячем «действие:номер задачи»,
    чтобы бот понял, какая именно кнопка нажата и к какой задаче она относится.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{task_id}")],
            [
                InlineKeyboardButton(text="⏰ +10 минут", callback_data=f"snooze:{task_id}:10"),
                InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze:{task_id}:60"),
            ],
        ]
    )


async def process_reminders(now):
    """
    Один «проход» по базе:
    1) присылает предупреждения «заранее», время которых наступило;
    2) присылает основные напоминания;
    3) для повторяющихся задач сразу назначает следующее срабатывание.
    """
    now_ts = int(now.timestamp())

    # --- 1. Предупреждения заранее ---
    due_advances = run_db(
        "SELECT a.id, t.chat_id, t.text, t.remind_at, a.minutes "
        "FROM advances a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.sent = 0 AND a.fire_at <= ? AND t.sent = 0 AND t.remind_at > ?",
        (now_ts, now_ts),
        fetch=True,
    )
    for adv_id, chat_id, text, remind_at, minutes in due_advances:
        try:
            when = datetime.fromtimestamp(remind_at, TZ)
            await bot.send_message(
                chat_id,
                f"🔔 Скоро ({fmt_before(minutes)}):\n{text}\n"
                f"Начало: {when.strftime('%d.%m.%Y в %H:%M')}",
            )
            run_db("UPDATE advances SET sent = 1 WHERE id = ?", (adv_id,))
        except Exception as error:
            print("Ошибка при отправке предупреждения:", error)

    # --- 2 и 3. Основные напоминания ---
    due_tasks = run_db(
        "SELECT id, chat_id, text, remind_at, repeat_rule, advance_min FROM tasks "
        "WHERE sent = 0 AND remind_at <= ?",
        (now_ts,),
        fetch=True,
    )
    for task_id, chat_id, text, remind_at, repeat_rule, advance_min in due_tasks:
        try:
            message_text = f"⏰ Напоминание:\n{text}"
            next_time = None
            if repeat_rule:
                planned = datetime.fromtimestamp(remind_at, TZ)
                next_time = next_occurrence(repeat_rule, planned.hour, planned.minute, now)
                if next_time:
                    message_text += f"\n🔁 Следующее: {short_when(next_time)}"

            await bot.send_message(
                chat_id, message_text, reply_markup=reminder_keyboard(task_id)
            )

            if next_time:
                # Двигаем задачу на следующее срабатывание и заново планируем предупреждения
                run_db(
                    "UPDATE tasks SET remind_at = ?, sent = 0 WHERE id = ?",
                    (int(next_time.timestamp()), task_id),
                )
                minutes_list = [int(x) for x in advance_min.split(",") if x]
                plan_advances(task_id, next_time, minutes_list, now)
            else:
                run_db("UPDATE tasks SET sent = 1 WHERE id = ?", (task_id,))
        except Exception as error:
            print("Ошибка при отправке напоминания:", error)


async def send_digests(now):
    """Утренняя сводка: в назначенное время присылает задачи на сегодня."""
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    users = run_db(
        "SELECT chat_id, digest_time FROM users WHERE digest_on = 1 AND last_digest != ?",
        (today,),
        fetch=True,
    )
    for chat_id, digest_time in users:
        if hhmm < digest_time:
            continue
        # Сначала помечаем, что сводка за сегодня обработана, чтобы не прислать дважды
        run_db("UPDATE users SET last_digest = ? WHERE chat_id = ?", (today, chat_id))
        body = render_today(chat_id, now)
        if body is None:
            continue  # задач на сегодня нет — не беспокоим
        try:
            await bot.send_message(
                chat_id,
                f"🌅 Доброе утро! Задачи на сегодня ({WD_SHORT[now.weekday()]} {now.strftime('%d.%m')}):\n\n{body}",
            )
        except Exception as error:
            print("Ошибка при отправке сводки:", error)


async def reminder_loop():
    """
    Работает в фоне всё время, пока запущен бот.
    Каждые 20 секунд заглядывает в базу: не пора ли что-то напомнить или прислать сводку.
    """
    while True:
        try:
            now = now_local()
            await process_reminders(now)
            await send_digests(now)
        except Exception as error:
            # Если что-то пошло не так (например, нет интернета), бот не падает,
            # а просто пишет ошибку в терминал и пробует снова через 20 секунд
            print("Ошибка в цикле напоминаний:", error)
        await asyncio.sleep(20)


# ======================= КОМАНДЫ БОТА =======================

@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.chat.id)
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def show_help(message: Message):
    ensure_user(message.chat.id)
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@dp.message(Command("list"))
@dp.message(F.text == BTN_LIST)
async def show_list(message: Message):
    ensure_user(message.chat.id)
    text, keyboard = render_list(message.chat.id)
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def show_today(message: Message):
    ensure_user(message.chat.id)
    body = render_today(message.chat.id, now_local())
    if body is None:
        await message.answer("На сегодня задач больше нет.")
    else:
        await message.answer(f"Задачи на сегодня:\n\n{body}")


@dp.message(Command("digest"))
async def digest_settings(message: Message, command: CommandObject):
    chat_id = message.chat.id
    ensure_user(chat_id)
    arg = (command.args or "").strip().lower()

    if arg in ("off", "выкл"):
        set_digest(chat_id, False)
        await message.answer("Утренняя сводка выключена. Включить: /digest on")
    elif arg in ("on", "вкл"):
        set_digest(chat_id, True)
        await message.answer("Утренняя сводка включена.")
    elif re.fullmatch(r"\d{1,2}:\d{2}", arg):
        hour, minute = (int(x) for x in arg.split(":"))
        if hour > 23 or minute > 59:
            await message.answer("Время указано неверно. Пример: /digest 7:30")
            return
        hhmm = f"{hour:02d}:{minute:02d}"
        set_digest(chat_id, True, hhmm)
        await message.answer(f"Буду присылать сводку каждое утро в {hhmm}.")
    else:
        row = run_db(
            "SELECT digest_on, digest_time FROM users WHERE chat_id = ?", (chat_id,), fetch=True
        )[0]
        state = f"включена, в {row[1]}" if row[0] else "выключена"
        await message.answer(
            f"Утренняя сводка: {state}.\n"
            "Поменять время: /digest 7:30\n"
            "Выключить: /digest off\n"
            "Включить: /digest on"
        )


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


# ---------- Кнопки под сообщениями ----------

# Нажата кнопка «🗑 №3» под списком задач
@dp.callback_query(F.data.startswith("del:"))
async def on_delete_button(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    delete_task(chat_id, task_id)
    text, keyboard = render_list(chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass  # список не изменился (например, нажали дважды), это не страшно
    await callback.answer("Удалено 🗑")


# Нажата кнопка «Выполнено» под напоминанием
@dp.callback_query(F.data.startswith("done:"))
async def on_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    info = get_task_info(callback.message.chat.id, task_id)
    if info is None:
        await callback.answer("Эта задача уже удалена", show_alert=True)
        return
    # Меняем текст сообщения и убираем кнопки, чтобы нельзя было нажать повторно
    await callback.message.edit_text(f"✅ Выполнено:\n{info[0]}")
    await callback.answer()


# Нажата кнопка «+10 минут» или «+1 час» под напоминанием
@dp.callback_query(F.data.startswith("snooze:"))
async def on_snooze(callback: CallbackQuery):
    _, task_id, minutes = callback.data.split(":")
    task_id, minutes = int(task_id), int(minutes)
    chat_id = callback.message.chat.id
    info = get_task_info(chat_id, task_id)
    if info is None:
        await callback.answer("Эта задача уже удалена", show_alert=True)
        return
    snooze_task(chat_id, task_id, minutes)
    new_time = now_local() + timedelta(minutes=minutes)
    label = "10 минут" if minutes == 10 else "1 час"
    await callback.message.edit_text(
        f"⏰ Отложено на {label} (до {new_time.strftime('%H:%M')}):\n{info[0]}"
    )
    await callback.answer()


# ---------- Новая задача ----------

# Любой другой текст — считаем, что это новая задача
@dp.message(F.text)
async def handle_task(message: Message):
    ensure_user(message.chat.id)
    result = parse_task(message.text)

    if result is None:
        await message.answer(
            "Не нашёл в сообщении дату. Попробуй так:\n"
            "Позвонить Ивану завтра в 15:00\n\n"
            "Все примеры: /help"
        )
        return

    if result["advance_missing"]:
        await message.answer(
            "Заранее — это за сколько? Добавь, например:\n"
            "за 30 минут, за 1 час, за 2 дня, за неделю"
        )
        return

    task, when = result["task"], result["when"]

    if when is None:
        await message.answer("Укажи время в формате 15:00, например: завтра в 15:00")
        return

    if when < now_local():
        await message.answer("Это время уже прошло. Укажи время в будущем.")
        return

    if not task:
        task = "Напоминание"

    _, made, skipped = add_task(
        message.chat.id, task, when, result["repeat"], result["advance"]
    )

    lines = ["Записал ✅", f"Задача: {task}"]
    if result["repeat"]:
        lines.append(f"Повтор: {describe_repeat(result['repeat'])}")
        lines.append(f"Первое напоминание: {when.strftime('%d.%m.%Y в %H:%M')} (Екатеринбург)")
    else:
        lines.append(f"Напомню: {when.strftime('%d.%m.%Y в %H:%M')} (Екатеринбург)")
    if made:
        lines.append("🔔 Заранее: " + ", ".join(fmt_before(m) for m in made))
    if skipped:
        lines.append(
            "Не поставил (время уже прошло): " + ", ".join(fmt_before(m) for m in skipped)
        )
    await message.answer("\n".join(lines))


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
