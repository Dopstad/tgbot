"""
Пошаговые мастера (экраны): создание задач и изменение уже созданных.
Здесь только расчёты и сборка экранов. Нажатия обрабатывает handlers/wizard.py.

Данные мастера (data) хранят всё выбранное:
  mode    — какой мастер идёт (см. STEPS)
  kind    — вид задачи: once (разовая), daily, weekly, monthly
  i       — номер текущего шага в STEPS[mode]
  custom  — "date" или "time", если пользователь вводит их руками
  text, day, hour, minute, mday, wdays, advance — выбранные значения
  task_id — номер задачи, если мы её изменяем
"""
from datetime import date, datetime, timedelta

from app import core
from app.core import TZ
from app.dates import (
    WD_SHORT, day_label, describe_repeat, next_occurrence, rule_days, rule_kind,
    weekdays_rule,
)
from app.db import parse_advance
from app.ui import BACK_ROW, btn, grid, markup

# Шаги каждого мастера. Первые четыре создают задачу, остальные меняют существующую.
STEPS = {
    "once": ["text", "day", "hour", "minute", "advance"],
    "daily": ["text", "hour", "minute", "advance"],
    "weekly": ["text", "wdays", "hour", "minute", "advance"],
    "monthly": ["text", "mday", "hour", "minute", "advance"],
    "edit_text": ["text"],
    "edit_time": ["hour", "minute"],
    "edit_day": ["day"],
    "edit_wdays": ["wdays"],
    "edit_mday": ["mday"],
    "edit_advance": ["advance"],
}
TITLES = {
    "once": "➕ Новая задача",
    "daily": "🔁 Ежедневная задача",
    "weekly": "📅 Повтор по дням недели",
    "monthly": "📆 Ежемесячная задача",
    "edit_text": "✏️ Изменить текст",
    "edit_time": "🕐 Изменить время",
    "edit_day": "📅 Изменить день",
    "edit_wdays": "📅 Изменить дни недели",
    "edit_mday": "📆 Изменить число месяца",
    "edit_advance": "🔔 Изменить «заранее»",
}
CREATE_MODES = ("once", "daily", "weekly", "monthly")

ADVANCE_CHOICES = [10, 30, 60, 120, 1440, 2880, 10080]
ADVANCE_LABELS = {
    10: "10 минут", 30: "30 минут", 60: "1 час", 120: "2 часа",
    1440: "1 день", 2880: "2 дня", 10080: "1 неделя",
}
WEEKDAY_PRESETS = {"work": [0, 1, 2, 3, 4], "weekend": [5, 6], "all": list(range(7))}


def new_data(mode):
    """Пустые данные для создания новой задачи."""
    return {"mode": mode, "kind": mode, "i": 0, "custom": None, "advance": []}


def edit_data(task, field):
    """
    Данные для изменения существующей задачи. Всё текущее подставляем заранее,
    чтобы меняя одно, не потерять остальное.
    field: text, time, when (день/дни/число, зависит от вида задачи), adv.
    """
    task_id, text, remind_at, rule, advance_min = task
    when = datetime.fromtimestamp(remind_at, TZ)
    kind = rule_kind(rule)
    if field == "when":
        mode = {"once": "edit_day", "weekly": "edit_wdays", "monthly": "edit_mday"}[kind]
    else:
        mode = {"text": "edit_text", "time": "edit_time", "adv": "edit_advance"}[field]
    data = {
        "mode": mode, "kind": kind, "i": 0, "custom": None, "task_id": task_id,
        "text": text, "day": when.date().isoformat(), "hour": when.hour, "minute": when.minute,
        "advance": parse_advance(advance_min),
    }
    if kind == "weekly":
        data["wdays"] = sorted(rule_days(rule))
    if kind == "monthly":
        data["mday"] = int(rule[6:])
    return data


def wizard_rule(data):
    """Правило повтора из выбранного."""
    kind = data["kind"]
    if kind == "daily":
        return "daily"
    if kind == "weekly":
        return weekdays_rule(data.get("wdays", []))
    if kind == "monthly":
        return f"month:{data['mday']}"
    return ""


def wizard_when(data, now):
    """Когда сработает задача по выбранному. Возвращает (время, правило повтора)."""
    hour, minute = data["hour"], data["minute"]
    if data["kind"] == "once":
        d = date.fromisoformat(data["day"])
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ), ""
    rule = wizard_rule(data)
    return next_occurrence(rule, hour, minute, now), rule


def wizard_summary(data):
    """Что уже выбрано: показываем над кнопками."""
    lines = []
    kind = data["kind"]
    if data.get("text"):
        lines.append(f"Задача: {data['text']}")
    if kind == "daily":
        lines.append("Повтор: каждый день")
    if kind == "weekly" and data.get("wdays"):
        lines.append("Повтор: " + describe_repeat(weekdays_rule(data["wdays"])))
    if kind == "monthly" and data.get("mday"):
        lines.append("Число: последний день месяца" if data["mday"] >= 31 else f"Число: {data['mday']}")
    if kind == "once" and data.get("day"):
        lines.append(f"День: {day_label(date.fromisoformat(data['day']))}")
    if data.get("hour") is not None:
        minute = f"{data['minute']:02d}" if data.get("minute") is not None else "__"
        lines.append(f"Время: {data['hour']:02d}:{minute}")
    return "\n".join(lines)


def wizard_screen(data, note=""):
    """Экран текущего шага мастера: (текст, кнопки)."""
    mode, i, kind = data["mode"], data["i"], data["kind"]
    steps = STEPS[mode]
    step = steps[i]
    custom = data.get("custom")
    now = core.now_local()
    today = now.date()
    is_last = i == len(steps) - 1

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
        if mode == "edit_text":
            lines.append("✍️ Напиши новый текст задачи.")
        else:
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
    elif step == "wdays":
        lines.append("📅 В какие дни повторять? Отметь дни и нажми кнопку внизу.")
        chosen = set(data.get("wdays", []))
        rows.append([
            btn("Будни", "w:wdp:work"), btn("Выходные", "w:wdp:weekend"), btn("Все дни", "w:wdp:all"),
        ])
        rows += grid(
            [btn(("✅ " if d in chosen else "") + WD_SHORT[d].capitalize(), f"w:wd:{d}") for d in range(7)],
            4,
        )
        rows.append([btn("💾 Готово" if is_last else "Дальше ➡️", "w:wnext")])
    elif step == "hour":
        lines.append("🕐 Во сколько? Выбери час.")
        hours = range(24)
        if kind == "once" and data.get("day") == today.isoformat():
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
    if data.get("task_id"):
        rows.append([btn("↩️ К задаче", f"t:{data['task_id']}")])
    rows.append(BACK_ROW)

    return "\n".join(lines).strip(), markup(rows)
