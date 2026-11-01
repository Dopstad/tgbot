"""
Экраны меню: что написано и какие кнопки. Здесь только сборка текста и кнопок,
ничего не отправляется (отправкой занимается nav.py).
"""
from datetime import datetime

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app import db
from app.core import TZ
from app.dates import (
    WD_SHORT, advance_text, describe_repeat, fmt_before, rule_kind, task_line,
)

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
    "/list — все задачи (нажми на номер, чтобы изменить или удалить)\n"
    "/today — задачи на сегодня\n"
    "/del 3 — удалить задачу с номером 3 из списка (или напиши «удали 3»)\n"
    "/digest — утренняя сводка: /digest 7:30, /digest off"
)


# ---------------------------- кнопки ----------------------------

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


def reminder_keyboard(task_id):
    """
    Кнопки под напоминанием. В callback_data прячем «действие:номер задачи»,
    чтобы бот понял, какая кнопка нажата и к какой задаче она относится.
    """
    return markup([
        [btn("✅ Выполнено", f"done:{task_id}")],
        [btn("⏰ +10 минут", f"snooze:{task_id}:10"), btn("⏰ +1 час", f"snooze:{task_id}:60")],
    ])


# ---------------------------- экраны ----------------------------

def main_menu_screen():
    """Главное меню."""
    rows = [
        [btn("📋 Список задач", "m:list")],
        [btn("➕ Новая задача", "m:new")],
        [btn("🌅 Ежедневный дайджест", "m:digest")],
        [btn("🔁 Добавить ежедневную задачу", "m:daily")],
        [btn("📅 Повтор по дням недели", "m:weekly")],
        [btn("📆 Добавить ежемесячную задачу", "m:monthly")],
    ]
    return "📱 Главное меню\n\nВыбери действие:", markup(rows)


def render_today(chat_id, now):
    """Задачи на сегодня. Если их нет, вернёт None."""
    tasks = db.get_tasks_for_day(chat_id, now.date())
    if not tasks:
        return None
    lines = []
    for _, text, remind_at, repeat_rule, _adv in tasks:
        when = datetime.fromtimestamp(remind_at, TZ)
        mark = " 🔁" if repeat_rule else ""
        lines.append(f"{when.strftime('%H:%M')} — {text}{mark}")
    return "\n".join(lines)


def list_screen(chat_id):
    """Экран «Список задач». Номера идут по порядку: 1, 2, 3..., без «дыр»."""
    tasks = db.get_tasks(chat_id)
    if not tasks:
        return "📋 Список задач\n\nАктивных задач нет.", markup([BACK_ROW])

    shown = tasks[:25]
    lines = ["📋 Список задач\n"]
    lines += [task_line(position, task) for position, task in enumerate(shown, start=1)]
    if len(tasks) > len(shown):
        lines.append(f"\n...и ещё {len(tasks) - len(shown)}")
    lines.append("\nНажми на номер, чтобы изменить или удалить задачу.")

    buttons = [btn(f"№{position}", f"t:{task[0]}") for position, task in enumerate(shown, start=1)]
    return "\n".join(lines), markup(grid(buttons, 4) + [BACK_ROW])


def task_card_screen(chat_id, task_id, note=""):
    """Карточка одной задачи: подробности и кнопки «изменить / удалить». None, если задачи нет."""
    task = db.get_task(chat_id, task_id)
    if task is None:
        return None
    _, text, remind_at, rule, advance_min = task
    position = db.get_position(chat_id, task_id)
    when = datetime.fromtimestamp(remind_at, TZ)

    lines = [f"✏️ Задача №{position}", "", text, ""]
    lines.append(f"🕐 {'Ближайшее' if rule else 'Когда'}: {when.strftime('%d.%m.%Y %H:%M')}")
    if rule:
        lines.append("🔁 " + describe_repeat(rule))
    lines.append("🔔 " + (advance_text(advance_min) if advance_min else "заранее не предупреждать"))
    if note:
        lines += ["", note]

    when_label = {"once": "📅 День", "weekly": "📅 Дни недели", "monthly": "📆 Число"}.get(
        rule_kind(rule)
    )
    second_row = [btn("🔔 Заранее", f"te:{task_id}:adv")]
    if when_label:
        second_row.insert(0, btn(when_label, f"te:{task_id}:when"))
    rows = [
        [btn("✏️ Текст", f"te:{task_id}:text"), btn("🕐 Время", f"te:{task_id}:time")],
        second_row,
        [btn("🗑 Удалить", f"td:{task_id}")],
        [btn("↩️ К списку", "m:list")],
        BACK_ROW,
    ]
    return "\n".join(lines), markup(rows)


def delete_confirm_screen(chat_id, task_id):
    """«Точно удалить?» Возвращает None, если задачи нет."""
    task = db.get_task(chat_id, task_id)
    if task is None:
        return None
    text = f"🗑 Удалить задачу?\n\n{task[1]}"
    rows = [[btn("🗑 Да, удалить", f"tdc:{task_id}"), btn("↩️ Нет", f"t:{task_id}")]]
    return text, markup(rows)


def digest_screen(chat_id, note=""):
    """Экран «Ежедневный дайджест»."""
    on, hhmm = db.get_digest(chat_id)
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
