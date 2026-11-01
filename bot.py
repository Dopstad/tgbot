import asyncio
import calendar
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
    if rule.startswith("month:"):
        day = int(rule[6:])
        return "каждый месяц в последний день" if day >= 31 else f"каждый месяц, {day} числа"
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
    if rule.startswith("month:"):
        # Ежемесячно: если числа в месяце нет (30 февраля), берём последний день месяца
        wanted = int(rule[6:])
        year, month = after.year, after.month
        for _ in range(14):
            last_day = calendar.monthrange(year, month)[1]
            candidate = datetime(year, month, min(wanted, last_day), hour, minute, tzinfo=TZ)
            if candidate > after:
                return candidate
            month += 1
            if month == 13:
                year, month = year + 1, 1
        return None
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

# Ежемесячно: «каждое 15 число», «каждый месяц 15 числа», «15 числа каждого месяца»
RE_MONTHLY_A = re.compile(
    r"\b(?:кажд\w+(?:\s+месяц\w*)?|ежемесячно)\s+(?:в\s+)?(\d{1,2})(?:-?го|-?е)?\s+(?:числа|число)\b",
    FLAGS,
)
RE_MONTHLY_B = re.compile(
    r"\b(\d{1,2})(?:-?го|-?е)?\s+(?:числа|число)\s+кажд\w+\s+месяц\w*", FLAGS
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


def find_repeat(text):
    """
    Ищет в тексте повтор. Возвращает (правило, найденный кусок) или ("", None).
    Правила: "daily", "wd:0,2,4" (дни недели), "month:15" (число месяца).
    """
    m = RE_MONTHLY_A.search(text) or RE_MONTHLY_B.search(text)
    if m and 1 <= int(m.group(1)) <= 31:
        return f"month:{int(m.group(1))}", m

    m = RE_EVERY_DAY.search(text)
    if m:
        return "daily", m

    m = RE_WORKDAYS.search(text)
    if m:
        return "wd:0,1,2,3,4", m

    m = RE_WEEKENDS.search(text)
    if m:
        return "wd:5,6", m

    m = RE_REPEAT_DAYS.search(text)
    if m:
        days = sorted({weekday_index(w) for w in re.findall(WD_ANY, m.group(1), FLAGS)})
        return "wd:" + ",".join(str(d) for d in days), m

    return "", None


def parse_task(text):
    """
    Разбирает фразу: суть задачи, время, повтор и предупреждения «заранее».
    Возвращает None, если ничего про время найти не удалось,
    иначе словарь (см. make_result).
    """
    now = now_local()
    base = text

    # ---- 1. Повтор: «каждый день», «по понедельникам», «каждое 15 число» ----
    repeat, m = find_repeat(base)
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

BTN_MENU = "📱 Меню"  # кнопка внизу экрана, открывает главное меню
# Старые кнопки нижней панели. Оставляем, чтобы они не ломались, если ещё висят в чате.
BTN_LIST = "📋 Мои задачи"
BTN_TODAY = "📅 На сегодня"
BTN_HELP = "ℹ️ Как пользоваться"

HELP_TEXT = (
    "Я бот-напоминалка. Меню открывается кнопкой «📱 Меню» внизу или командой /menu.\n\n"
    "Задачи можно ставить и обычными фразами:\n"
    "• Позвонить Ивану завтра в 15:00\n"
    "• Тренировка в пятницу в 19:00\n"
    "• Планёрка в понедельник на следующей неделе в 9:00\n"
    "• Купить воду через 2 часа\n\n"
    "Заранее (можно несколько сразу):\n"
    "• Созвон завтра в 15:00, напомни за 30 минут\n"
    "• Конференция в пятницу в 10:00 за 1 час и за 2 дня\n\n"
    "Повторы:\n"
    "• Зарядка каждый день в 7:00\n"
    "• Планёрка каждый понедельник в 9:00\n"
    "• Тренировка по понедельникам, средам и пятницам в 19:00\n"
    "• Отчёт по будням в 17:30\n"
    "• Оплатить аренду каждое 15 число в 10:00\n\n"
    "Команды:\n"
    "/menu — главное меню\n"
    "/list — все задачи\n"
    "/today — задачи на сегодня\n"
    "/del 3 — удалить задачу №3 (или напиши «удали 3»)\n"
    "/digest — утренняя сводка: /digest 7:30, /digest off"
)

# Последний «экран» меню в каждом чате (номер сообщения).
# Когда показываем новый экран, старый удаляем, чтобы чат не захламлялся.
LAST_SCREEN = {}


def btn(text, data):
    """Одна кнопка под сообщением."""
    return InlineKeyboardButton(text=text, callback_data=data)


def grid(buttons, per_row):
    """Раскладывает кнопки по рядам."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK_ROW = [btn("⬅️ Назад в меню", "m:main")]


def reply_menu():
    """Одна кнопка внизу экрана: открывает меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_MENU)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_screen():
    """Главное меню."""
    text = "📱 Главное меню\n\nВыбери действие:"
    rows = [
        [btn("📋 Список задач", "m:list")],
        [btn("➕ Новая задача", "m:new")],
        [btn("🌅 Ежедневный дайджест", "m:digest")],
        [btn("🔁 Добавить ежедневную задачу", "m:daily")],
        [btn("📆 Добавить ежемесячную задачу", "m:monthly")],
    ]
    return text, markup(rows)


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


def list_screen(chat_id):
    """Экран «Список задач»: список и кнопки удаления."""
    tasks = get_tasks(chat_id)
    if not tasks:
        return "📋 Список задач\n\nАктивных задач нет.", markup([BACK_ROW])

    shown = tasks[:25]
    lines = ["📋 Список задач\n"]
    lines += [task_line(*task) for task in shown]
    if len(tasks) > len(shown):
        lines.append(f"\n...и ещё {len(tasks) - len(shown)}")
    lines.append("\nУдалить: нажми кнопку с номером или напиши /del номер")

    buttons = [btn(f"🗑 №{task[0]}", f"del:{task[0]}") for task in shown]
    return "\n".join(lines), markup(grid(buttons, 4) + [BACK_ROW])


def digest_screen(chat_id, note=""):
    """Экран «Ежедневный дайджест»."""
    ensure_user(chat_id)
    on, hhmm = run_db(
        "SELECT digest_on, digest_time FROM users WHERE chat_id = ?", (chat_id,), fetch=True
    )[0]
    state = f"включён, каждое утро в {hhmm}" if on else "выключен"
    text = (
        "🌅 Ежедневный дайджест\n\n"
        "Каждое утро бот присылает список задач на сегодня (если они есть).\n\n"
        f"Сейчас: {state}"
    )
    if note:
        text += f"\n\n{note}"
    rows = [
        [btn("🔕 Выключить" if on else "🔔 Включить", "dg:toggle")],
        [btn("🕐 Изменить время", "dg:time")],
        BACK_ROW,
    ]
    return text, markup(rows)


# ---------- Пошаговые мастера: новая, ежедневная, ежемесячная задача ----------

class Wizard(StatesGroup):
    text = State()         # ждём текст задачи
    custom_date = State()  # ждём дату, написанную руками
    custom_time = State()  # ждём время, написанное руками
    choosing = State()     # выбор кнопками


# Шаги для каждого вида задач
STEPS = {
    "once": ["text", "day", "hour", "minute", "advance"],
    "daily": ["text", "hour", "minute", "advance"],
    "monthly": ["text", "mday", "hour", "minute", "advance"],
}
TITLES = {
    "once": "➕ Новая задача",
    "daily": "🔁 Ежедневная задача",
    "monthly": "📆 Ежемесячная задача",
}
ADVANCE_CHOICES = [10, 30, 60, 120, 1440, 2880, 10080]
ADVANCE_LABELS = {
    10: "10 минут", 30: "30 минут", 60: "1 час", 120: "2 часа",
    1440: "1 день", 2880: "2 дня", 10080: "1 неделя",
}


def day_label(d):
    """«Вт 03.11.2026»."""
    return f"{WD_SHORT[d.weekday()].capitalize()} {d.strftime('%d.%m.%Y')}"


def wizard_summary(data):
    """Что уже выбрано: показываем над кнопками."""
    lines = []
    if data.get("text"):
        lines.append(f"Задача: {data['text']}")
    if data["mode"] == "daily":
        lines.append("Повтор: каждый день")
    if data["mode"] == "monthly" and data.get("mday"):
        lines.append("Число: последний день месяца" if data["mday"] >= 31 else f"Число: {data['mday']}")
    if data.get("day"):
        lines.append(f"День: {day_label(date.fromisoformat(data['day']))}")
    if data.get("hour") is not None:
        minute = f"{data['minute']:02d}" if data.get("minute") is not None else "__"
        lines.append(f"Время: {data['hour']:02d}:{minute}")
    return "\n".join(lines)


def wizard_screen(data, note=""):
    """Экран текущего шага мастера: (текст, кнопки)."""
    mode, i = data["mode"], data["i"]
    step = STEPS[mode][i]
    custom = data.get("custom")
    now = now_local()
    today = now.date()

    lines = [TITLES[mode], ""]
    summary = wizard_summary(data)
    if summary:
        lines += [summary, ""]
    if note:
        lines += [note, ""]

    rows = []

    if custom == "date":
        lines.append("✍️ Напиши дату: ДД.ММ или ДД.ММ.ГГГГ\nНапример: 15.11")
    elif custom == "time":
        lines.append("✍️ Напиши время: ЧЧ:ММ\nНапример: 15:40")
    elif step == "text":
        lines.append(
            "✍️ Напиши, что напомнить. Только суть, без даты: "
            "дату и время выберешь кнопками.\nНапример: Позвонить Ивану"
        )
    elif step == "day":
        lines.append("📅 На какой день?")
        quick = [("Сегодня", 0), ("Завтра", 1), ("Послезавтра", 2)]
        rows.append([btn(name, f"w:day:{(today + timedelta(days=n)).isoformat()}") for name, n in quick])
        later = []
        for n in range(3, 10):
            d = today + timedelta(days=n)
            later.append(btn(f"{WD_SHORT[d.weekday()].capitalize()} {d.strftime('%d.%m')}", f"w:day:{d.isoformat()}"))
        rows += grid(later, 4)
        rows.append([btn("✍️ Другая дата", "w:daycustom")])
    elif step == "hour":
        lines.append("🕐 Во сколько? Выбери час.")
        hours = range(24)
        if mode == "once" and data.get("day") == today.isoformat():
            hours = range(now.hour, 24)
        rows += grid([btn(f"{h:02d}", f"w:h:{h}") for h in hours], 6)
        rows.append([btn("✍️ Ввести время вручную", "w:timecustom")])
    elif step == "minute":
        lines.append("🕐 Выбери минуты.")
        hour = data["hour"]
        rows += grid([btn(f"{hour:02d}:{m:02d}", f"w:min:{m}") for m in range(0, 60, 5)], 4)
        rows.append([btn("✍️ Ввести время вручную", "w:timecustom")])
    elif step == "mday":
        lines.append(
            "📆 Какого числа каждый месяц?\n"
            "Если в месяце такого числа нет (например, 30 февраля), "
            "напоминание придёт в последний день месяца."
        )
        rows += grid([btn(str(n), f"w:md:{n}") for n in range(1, 32)], 7)
        rows.append([btn("🔚 Последний день месяца", "w:md:31")])
    elif step == "advance":
        lines.append(
            "🔔 Предупредить заранее? Отметь нужное (можно несколько) и нажми «Сохранить». "
            "Если предупреждения не нужны, просто нажми «Сохранить»."
        )
        chosen = set(data.get("advance", []))
        buttons = [
            btn(("✅ " if m in chosen else "") + ADVANCE_LABELS[m], f"w:adv:{m}")
            for m in ADVANCE_CHOICES
        ]
        rows += grid(buttons, 3)
        rows.append([btn("💾 Сохранить", "w:save")])

    # Кнопки возврата
    if custom:
        rows.append([btn("↩️ Выбрать кнопками", "w:cur")])
    elif i > 0:
        rows.append([btn("↩️ Предыдущий шаг", "w:prev")])
    rows.append(BACK_ROW)

    return "\n".join(lines).strip(), markup(rows)


def confirmation_text(task, when, repeat, made, skipped):
    """Ответ «Записал ✅» со всеми подробностями."""
    lines = ["Записал ✅", f"Задача: {task}"]
    if repeat:
        lines.append(f"Повтор: {describe_repeat(repeat)}")
        lines.append(f"Первое напоминание: {when.strftime('%d.%m.%Y в %H:%M')} (Екатеринбург)")
    else:
        lines.append(f"Напомню: {when.strftime('%d.%m.%Y в %H:%M')} (Екатеринбург)")
    if made:
        lines.append("🔔 Заранее: " + ", ".join(fmt_before(m) for m in made))
    if skipped:
        lines.append("Не поставил (время уже прошло): " + ", ".join(fmt_before(m) for m in skipped))
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


# ======================= ЭКРАНЫ: ПОКАЗ И УДАЛЕНИЕ СТАРЫХ =======================

async def delete_quietly(chat_id, message_id):
    """Удаляет сообщение. Если не вышло (например, оно старше 48 часов), молча идёт дальше."""
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except Exception:
        return False


async def show_screen(chat_id, text, keyboard, old_message=None):
    """
    Показывает новый экран и удаляет старый: то сообщение, с кнопки которого
    пришло нажатие, и последний экран меню в этом чате.
    """
    old_ids = []
    if old_message is not None:
        old_ids.append(old_message.message_id)
    last = LAST_SCREEN.get(chat_id)
    if last and last not in old_ids:
        old_ids.append(last)

    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    LAST_SCREEN[chat_id] = sent.message_id

    for message_id in old_ids:
        if not await delete_quietly(chat_id, message_id):
            # Удалить не вышло: хотя бы убираем кнопки, чтобы они не вводили в заблуждение
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                pass


async def open_main_menu(chat_id, old_message=None):
    text, keyboard = main_menu_screen()
    await show_screen(chat_id, text, keyboard, old_message)


async def show_wizard(chat_id, state, old_message=None, note=""):
    """Показывает текущий шаг мастера и выставляет нужное состояние."""
    data = await state.get_data()
    text, keyboard = wizard_screen(data, note)
    custom = data.get("custom")
    step = STEPS[data["mode"]][data["i"]]
    if custom == "date":
        await state.set_state(Wizard.custom_date)
    elif custom == "time":
        await state.set_state(Wizard.custom_time)
    elif step == "text":
        await state.set_state(Wizard.text)
    else:
        await state.set_state(Wizard.choosing)
    await show_screen(chat_id, text, keyboard, old_message)


async def start_wizard(chat_id, state, mode, old_message=None):
    await state.clear()
    await state.update_data(mode=mode, i=0, advance=[], custom=None)
    await show_wizard(chat_id, state, old_message)


def wizard_when(data, now):
    """Собирает дату и время задачи из выбранного. Возвращает (время, правило повтора)."""
    mode = data["mode"]
    hour, minute = data["hour"], data["minute"]
    if mode == "once":
        d = date.fromisoformat(data["day"])
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ), ""
    if mode == "daily":
        return next_occurrence("daily", hour, minute, now), "daily"
    rule = f"month:{data['mday']}"
    return next_occurrence(rule, hour, minute, now), rule


async def finish_wizard(callback, state, data):
    """Сохраняет задачу из мастера."""
    chat_id = callback.message.chat.id
    now = now_local()
    when, repeat = wizard_when(data, now)
    if when <= now:
        await callback.answer("Это время уже прошло. Выбери другое.", show_alert=True)
        return
    _, made, skipped = add_task(chat_id, data["text"], when, repeat, data.get("advance", []))
    await state.clear()
    keyboard = markup([[btn("➕ Ещё задачу", "m:new"), btn("📱 Меню", "m:main")]])
    await show_screen(
        chat_id,
        confirmation_text(data["text"], when, repeat, made, skipped),
        keyboard,
        callback.message,
    )
    await callback.answer("Сохранено ✅")


# ======================= КОМАНДЫ БОТА =======================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.chat.id)
    await message.answer(
        "Привет! Я бот-напоминалка.\n"
        "Меню открывается кнопкой «📱 Меню» внизу экрана или командой /menu.\n"
        "Фразами тоже можно, примеры: /help",
        reply_markup=reply_menu(),
    )
    await open_main_menu(message.chat.id)


@dp.message(Command("menu"))
@dp.message(F.text == BTN_MENU)
async def show_menu(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.chat.id)
    await delete_quietly(message.chat.id, message.message_id)  # убираем и сообщение «Меню»
    await open_main_menu(message.chat.id)


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def show_help(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.chat.id)
    await message.answer(HELP_TEXT, reply_markup=reply_menu())


@dp.message(Command("list"))
@dp.message(F.text == BTN_LIST)
async def show_list(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.chat.id)
    text, keyboard = list_screen(message.chat.id)
    await show_screen(message.chat.id, text, keyboard)


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def show_today(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.chat.id)
    body = render_today(message.chat.id, now_local())
    if body is None:
        await message.answer("На сегодня задач больше нет.")
    else:
        await message.answer(f"Задачи на сегодня:\n\n{body}")


@dp.message(Command("digest"))
async def digest_settings(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
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
        text, keyboard = digest_screen(chat_id)
        await show_screen(chat_id, text, keyboard)


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


# ---------- Ввод текста внутри мастеров ----------

@dp.message(Wizard.text, F.text)
async def wizard_text(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)  # убираем набранное, чтобы не засорять чат
    data = await state.get_data()
    if "mode" not in data:
        await state.clear()
        await open_main_menu(chat_id)
        return
    text = message.text.strip()
    if not text:
        return
    await state.update_data(text=text[:200], i=data["i"] + 1)
    await show_wizard(chat_id, state)


@dp.message(Wizard.custom_date, F.text)
async def wizard_custom_date(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)
    data = await state.get_data()
    if "mode" not in data:
        await state.clear()
        await open_main_menu(chat_id)
        return

    m = re.fullmatch(r"\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*", message.text)
    today = now_local().date()
    note = "⚠️ Не понял дату. Пример: 15.11 или 15.11.2026"
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            chosen = date(year, month, day)
            if not m.group(3) and chosen < today:
                chosen = date(year + 1, month, day)
            if chosen < today:
                note = "⚠️ Эта дата уже прошла. Напиши другую."
            else:
                await state.update_data(day=chosen.isoformat(), i=data["i"] + 1, custom=None)
                await show_wizard(chat_id, state)
                return
        except ValueError:
            note = "⚠️ Такой даты не существует. Пример: 15.11"
    await show_wizard(chat_id, state, note=note)


@dp.message(Wizard.custom_time, F.text)
async def wizard_custom_time(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await delete_quietly(chat_id, message.message_id)
    data = await state.get_data()
    if "mode" not in data:
        await state.clear()
        await open_main_menu(chat_id)
        return

    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", message.text)
    note = "⚠️ Не понял время. Пример: 15:40"
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        hour, minute = int(m.group(1)), int(m.group(2))
        probe = dict(data, hour=hour, minute=minute)
        if data["mode"] == "once" and wizard_when(probe, now_local())[0] <= now_local():
            note = "⚠️ Это время уже прошло. Напиши другое."
        else:
            advance_index = STEPS[data["mode"]].index("advance")
            await state.update_data(hour=hour, minute=minute, i=advance_index, custom=None)
            await show_wizard(chat_id, state)
            return
    await show_wizard(chat_id, state, note=note)


# Фраза вида «удали 3» / «удалить 3» / «удали задачу 3»
@dp.message(F.text.regexp(r"(?i)^\s*удал\w*\s+(задач\w*\s+)?(№\s*)?(\d+)\s*$"))
async def delete_by_text(message: Message):
    number = re.search(r"(\d+)\s*$", message.text).group(1)
    await do_delete(message, number)


# ---------- Кнопки под сообщениями ----------

# Кнопки главного меню: m:list, m:new, m:digest, m:daily, m:monthly, m:main
@dp.callback_query(F.data.startswith("m:"))
async def on_menu_button(callback: CallbackQuery, state: FSMContext):
    action = callback.data[2:]
    chat_id = callback.message.chat.id
    ensure_user(chat_id)

    if action in ("new", "daily", "monthly"):
        mode = {"new": "once", "daily": "daily", "monthly": "monthly"}[action]
        await start_wizard(chat_id, state, mode, callback.message)
    else:
        await state.clear()
        if action == "list":
            text, keyboard = list_screen(chat_id)
            await show_screen(chat_id, text, keyboard, callback.message)
        elif action == "digest":
            text, keyboard = digest_screen(chat_id)
            await show_screen(chat_id, text, keyboard, callback.message)
        else:
            await open_main_menu(chat_id, callback.message)
    await callback.answer()


# Кнопки экрана «Ежедневный дайджест»
@dp.callback_query(F.data.startswith("dg:"))
async def on_digest_button(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    action = parts[1]
    chat_id = callback.message.chat.id
    ensure_user(chat_id)
    await state.clear()

    if action == "toggle":
        on = run_db("SELECT digest_on FROM users WHERE chat_id = ?", (chat_id,), fetch=True)[0][0]
        set_digest(chat_id, not on)
        text, keyboard = digest_screen(chat_id)
        try:
            # Переключатель меняем «на месте», без нового сообщения
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    elif action == "time":
        rows = grid([btn(f"{h:02d}", f"dg:h:{h}") for h in range(24)], 6)
        rows.append([btn("↩️ К дайджесту", "m:digest")])
        rows.append(BACK_ROW)
        await show_screen(
            chat_id, "🌅 Ежедневный дайджест\n\n🕐 Во сколько присылать? Выбери час.",
            markup(rows), callback.message,
        )
    elif action == "h":
        hour = int(parts[2])
        rows = grid([btn(f"{hour:02d}:{m:02d}", f"dg:m:{hour}:{m}") for m in (0, 15, 30, 45)], 4)
        rows.append([btn("↩️ К дайджесту", "m:digest")])
        rows.append(BACK_ROW)
        await show_screen(
            chat_id, "🌅 Ежедневный дайджест\n\n🕐 Выбери минуты.", markup(rows), callback.message
        )
    elif action == "m":
        hhmm = f"{int(parts[2]):02d}:{int(parts[3]):02d}"
        set_digest(chat_id, True, hhmm)
        text, keyboard = digest_screen(chat_id, note=f"✅ Время сохранено: {hhmm}")
        await show_screen(chat_id, text, keyboard, callback.message)
    await callback.answer()


# Кнопки мастеров: w:day:..., w:h:..., w:min:..., w:md:..., w:adv:..., w:save и т.д.
@dp.callback_query(F.data.startswith("w:"))
async def on_wizard_button(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    data = await state.get_data()
    if "mode" not in data:
        # Бот перезапускался, и мастер забыл, на каком шаге вы были
        await callback.answer("Эта кнопка устарела, открываю меню")
        await open_main_menu(chat_id, callback.message)
        return

    parts = callback.data.split(":")
    action = parts[1]
    mode, i = data["mode"], data["i"]

    if action == "prev":
        await state.update_data(i=max(0, i - 1), custom=None)
    elif action == "cur":
        await state.update_data(custom=None)
    elif action == "day":
        await state.update_data(day=parts[2], i=i + 1)
    elif action == "daycustom":
        await state.update_data(custom="date")
    elif action == "h":
        await state.update_data(hour=int(parts[2]), i=i + 1)
    elif action == "md":
        await state.update_data(mday=int(parts[2]), i=i + 1)
    elif action == "timecustom":
        await state.update_data(custom="time")
    elif action == "min":
        minute = int(parts[2])
        probe = dict(data, minute=minute)
        if mode == "once" and wizard_when(probe, now_local())[0] <= now_local():
            await callback.answer("Это время уже прошло. Выбери другое.", show_alert=True)
            return
        await state.update_data(minute=minute, i=i + 1)
    elif action == "adv":
        minutes = int(parts[2])
        chosen = set(data.get("advance", []))
        chosen ^= {minutes}  # нажали: добавили, нажали ещё раз: убрали
        await state.update_data(advance=sorted(chosen, reverse=True))
        data = await state.get_data()
        _, keyboard = wizard_screen(data)
        try:
            # Галочки меняем «на месте», без нового сообщения
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    elif action == "save":
        await finish_wizard(callback, state, data)
        return

    await callback.answer()
    await show_wizard(chat_id, state, callback.message)


# Нажата кнопка «🗑 №3» под списком задач
@dp.callback_query(F.data.startswith("del:"))
async def on_delete_button(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    delete_task(chat_id, task_id)
    text, keyboard = list_screen(chat_id)
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


# ---------- Новая задача обычной фразой ----------

# Любой другой текст — считаем, что это новая задача
@dp.message(F.text)
async def handle_task(message: Message):
    ensure_user(message.chat.id)
    result = parse_task(message.text)

    if result is None:
        await message.answer(
            "Не нашёл в сообщении дату. Попробуй так:\n"
            "Позвонить Ивану завтра в 15:00\n\n"
            "Или открой меню: /menu. Все примеры: /help"
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
    await message.answer(confirmation_text(task, when, result["repeat"], made, skipped))


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
