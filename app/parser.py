"""
Разбор фраз: из «Позвонить Ивану завтра в 15:00, напомни за 30 минут»
получаем суть задачи, время, повтор и предупреждения «заранее».
Здесь нет ни базы, ни Telegram, только разбор текста.
"""
import re
from datetime import datetime, timedelta

from dateparser.search import search_dates

from app import core
from app.core import TZ
from app.dates import next_occurrence, weekdays_rule

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
        return weekdays_rule(days), m

    return "", None


def parse_task(text):
    """
    Разбирает фразу: суть задачи, время, повтор и предупреждения «заранее».
    Возвращает None, если ничего про время найти не удалось,
    иначе словарь (см. make_result).
    """
    now = core.now_local()
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


