"""
Даты и повторы: когда сработает следующее напоминание и как это написать словами.
Здесь нет ни базы, ни Telegram, только расчёты и тексты, поэтому файл легко проверять.
"""
import calendar
from datetime import datetime, timedelta

from app.core import TZ

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


# ---------------------------- правила повтора ----------------------------
# "daily"      — каждый день
# "wd:0,2,4"   — по дням недели (пн=0 ... вс=6)
# "month:15"   — каждый месяц 15 числа (31 = последний день месяца)

def weekdays_rule(days):
    """Набор дней недели -> правило повтора."""
    return "wd:" + ",".join(str(d) for d in sorted(set(days)))


def rule_days(rule):
    """Из правила повтора получает набор дней недели."""
    if rule.startswith("wd:"):
        return {int(x) for x in rule[3:].split(",") if x != ""}
    return set(range(7))


def rule_kind(rule):
    """Вид задачи по правилу: once, daily, weekly, monthly."""
    if not rule:
        return "once"
    if rule == "daily":
        return "daily"
    if rule.startswith("month:"):
        return "monthly"
    return "weekly"


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


# ---------------------------- тексты ----------------------------

def short_when(dt):
    """«пн 02.11 в 09:00»."""
    return f"{WD_SHORT[dt.weekday()]} {dt.strftime('%d.%m')} в {dt.strftime('%H:%M')}"


def day_label(d):
    """«Вт 03.11.2026»."""
    return f"{WD_SHORT[d.weekday()].capitalize()} {d.strftime('%d.%m.%Y')}"


def advance_text(advance_min):
    """«30,60» -> «за 1 час, за 30 минут»."""
    minutes = sorted({int(x) for x in advance_min.split(",") if x}, reverse=True)
    return ", ".join(fmt_before(m) for m in minutes)


def task_line(position, task):
    """Строка задачи для списка. position — номер в списке, task — строка из базы."""
    _, text, remind_at, repeat_rule, advance_min = task
    when = datetime.fromtimestamp(remind_at, TZ)
    line = f"№{position} — {when.strftime('%d.%m.%Y %H:%M')} — {text}"
    extras = []
    if repeat_rule:
        extras.append("🔁 " + describe_repeat(repeat_rule))
    if advance_min:
        extras.append("🔔 " + advance_text(advance_min))
    if extras:
        line += "\n      " + " · ".join(extras)
    return line
