"""
База данных (SQLite): всё, что читает и пишет задачи и настройки.
Больше ни один файл не лезет в базу напрямую, только через функции отсюда.

Таблица tasks — задачи:
  id          — внутренний номер (пользователю не показывается)
  chat_id     — чей это чат
  text        — суть задачи
  remind_at   — когда напомнить (метка времени)
  sent        — 0 = ещё не сработала, 1 = уже сработала
  repeat_rule — повтор: пусто, "daily", "wd:0,2,4" (дни недели, пн=0), "month:15"
  advance_min — за сколько минут предупреждать заранее: "30,60,2880"
Таблица advances — запланированные предупреждения «заранее»
Таблица users    — настройки утренней сводки
"""
import sqlite3
from datetime import datetime, timedelta

from app import core
from app.core import TZ


def run_db(query, params=(), fetch=False, want_id=False):
    """Открывает базу, выполняет один запрос и закрывает её."""
    conn = sqlite3.connect(core.DB_PATH)
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


# ---------------------------- задачи ----------------------------

def plan_advances(task_id, when, minutes_list, now=None):
    """
    Планирует предупреждения «заранее». Старые предупреждения этой задачи стираются.
    Если время предупреждения уже прошло, оно пропускается.
    Возвращает (созданные, пропущенные).
    """
    now = now or core.now_local()
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


def parse_advance(text):
    """«30,60» -> [60, 30]."""
    return sorted({int(x) for x in text.split(",") if x}, reverse=True)


def add_task(chat_id, text, when, repeat_rule="", advance=()):
    """Сохраняет новую задачу. Возвращает (номер, созданные предупреждения, пропущенные)."""
    advance_text = ",".join(str(m) for m in sorted(set(advance), reverse=True))
    task_id = run_db(
        "INSERT INTO tasks (chat_id, text, remind_at, repeat_rule, advance_min) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, text, int(when.timestamp()), repeat_rule, advance_text),
        want_id=True,
    )
    made, skipped = plan_advances(task_id, when, advance)
    return task_id, made, skipped


TASK_COLUMNS = "id, text, remind_at, repeat_rule, advance_min"


def get_tasks(chat_id):
    """Все ещё не сработавшие задачи чата по порядку времени."""
    return run_db(
        f"SELECT {TASK_COLUMNS} FROM tasks WHERE chat_id = ? AND sent = 0 "
        "ORDER BY remind_at, id",
        (chat_id,),
        fetch=True,
    )


def get_task(chat_id, task_id):
    """Одна несработавшая задача по внутреннему номеру, или None."""
    rows = run_db(
        f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ? AND chat_id = ? AND sent = 0",
        (task_id, chat_id),
        fetch=True,
    )
    return rows[0] if rows else None


def get_task_info(chat_id, task_id):
    """Текст и правило повтора задачи (даже если она уже сработала), или None."""
    rows = run_db(
        "SELECT text, repeat_rule FROM tasks WHERE id = ? AND chat_id = ?",
        (task_id, chat_id),
        fetch=True,
    )
    return rows[0] if rows else None


def get_position(chat_id, task_id):
    """Номер задачи в списке (1, 2, 3...) или None. Это тот номер, который видит пользователь."""
    for position, task in enumerate(get_tasks(chat_id), start=1):
        if task[0] == task_id:
            return position
    return None


def task_by_position(chat_id, position):
    """Задача по номеру из списка, или None."""
    tasks = get_tasks(chat_id)
    if 1 <= position <= len(tasks):
        return tasks[position - 1]
    return None


def get_tasks_for_day(chat_id, day):
    """Задачи на конкретный день (day — дата)."""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    return run_db(
        f"SELECT {TASK_COLUMNS} FROM tasks "
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


def update_task_text(chat_id, task_id, text):
    return run_db(
        "UPDATE tasks SET text = ? WHERE id = ? AND chat_id = ?", (text, task_id, chat_id)
    )


def update_task_schedule(chat_id, task_id, when, repeat_rule):
    """Меняет время и правило повтора, заново планирует предупреждения."""
    changed = run_db(
        "UPDATE tasks SET remind_at = ?, repeat_rule = ?, sent = 0 WHERE id = ? AND chat_id = ?",
        (int(when.timestamp()), repeat_rule, task_id, chat_id),
    )
    if changed:
        row = get_task(chat_id, task_id)
        plan_advances(task_id, when, parse_advance(row[4]))
    return changed


def update_task_advance(chat_id, task_id, minutes_list):
    """Меняет предупреждения «заранее». Возвращает (созданные, пропущенные)."""
    row = get_task(chat_id, task_id)
    if row is None:
        return [], []
    advance_text = ",".join(str(m) for m in sorted(set(minutes_list), reverse=True))
    run_db(
        "UPDATE tasks SET advance_min = ? WHERE id = ? AND chat_id = ?",
        (advance_text, task_id, chat_id),
    )
    return plan_advances(task_id, datetime.fromtimestamp(row[2], TZ), minutes_list)


def snooze_task(chat_id, task_id, minutes):
    """
    Откладывает задачу на N минут. Обычную переносим. Повторяющуюся не трогаем
    (иначе сломается расписание), а создаём рядом разовую копию.
    """
    info = get_task_info(chat_id, task_id)
    if info is None:
        return False
    text, repeat_rule = info
    new_time = core.now_local() + timedelta(minutes=minutes)
    if repeat_rule:
        add_task(chat_id, text, new_time)
    else:
        run_db(
            "UPDATE tasks SET remind_at = ?, sent = 0 WHERE id = ? AND chat_id = ?",
            (int(new_time.timestamp()), task_id, chat_id),
        )
        run_db("DELETE FROM advances WHERE task_id = ?", (task_id,))
    return True


def cleanup(now, keep_days=7):
    """Стирает давно сработавшие разовые задачи и их предупреждения, чтобы база не пухла."""
    border = int((now - timedelta(days=keep_days)).timestamp())
    run_db("DELETE FROM tasks WHERE sent = 1 AND remind_at < ?", (border,))
    run_db("DELETE FROM advances WHERE task_id NOT IN (SELECT id FROM tasks)")


# ---------------------------- сводка ----------------------------

def ensure_user(chat_id):
    """
    Запоминает чат для утренней сводки. Если человек появился уже после
    времени сводки, на сегодня её не присылаем, начнём со следующего утра.
    """
    if run_db("SELECT 1 FROM users WHERE chat_id = ?", (chat_id,), fetch=True):
        return
    now = core.now_local()
    late = now.strftime("%H:%M") >= core.DEFAULT_DIGEST_TIME
    run_db(
        "INSERT OR IGNORE INTO users (chat_id, last_digest) VALUES (?, ?)",
        (chat_id, now.strftime("%Y-%m-%d") if late else ""),
    )


def get_digest(chat_id):
    """(включена ли, время) сводки."""
    ensure_user(chat_id)
    on, hhmm = run_db(
        "SELECT digest_on, digest_time FROM users WHERE chat_id = ?", (chat_id,), fetch=True
    )[0]
    return bool(on), hhmm


def set_digest(chat_id, on, hhmm=None):
    """Включает/выключает сводку и меняет время."""
    if hhmm is None:
        hhmm = get_digest(chat_id)[1]
    ensure_user(chat_id)
    now = core.now_local()
    last = now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= hhmm else ""
    run_db(
        "UPDATE users SET digest_on = ?, digest_time = ?, last_digest = ? WHERE chat_id = ?",
        (1 if on else 0, hhmm, last, chat_id),
    )
