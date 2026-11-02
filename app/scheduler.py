"""
Фоновая работа: проверка базы каждые 20 секунд.
Присылает напоминания, предупреждения «заранее» и утреннюю сводку.
"""
import asyncio
import logging
from datetime import datetime

from app import core, db, ui
from app.core import TZ
from app.dates import fmt_before, next_occurrence, short_when, WD_SHORT

log = logging.getLogger(__name__)


async def process_reminders(now):
    """
    Один «проход» по базе:
    1) присылает предупреждения «заранее», время которых наступило;
    2) присылает основные напоминания;
    3) для повторяющихся задач сразу назначает следующее срабатывание.
    """
    now_ts = int(now.timestamp())

    # --- 1. Предупреждения заранее (только если само событие ещё впереди) ---
    due_advances = db.run_db(
        "SELECT a.id, t.chat_id, t.text, t.remind_at, a.minutes "
        "FROM advances a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.sent = 0 AND a.fire_at <= ? AND t.sent = 0 AND t.remind_at > ?",
        (now_ts, now_ts),
        fetch=True,
    )
    for adv_id, chat_id, text, remind_at, minutes in due_advances:
        try:
            when = datetime.fromtimestamp(remind_at, TZ)
            await core.bot.send_message(
                chat_id,
                f"🔔 Скоро ({fmt_before(minutes)}):\n{text}\n"
                f"Начало: {when.strftime('%d.%m.%Y в %H:%M')}",
            )
            db.run_db("UPDATE advances SET sent = 1 WHERE id = ?", (adv_id,))
        except Exception:
            log.exception("Не удалось отправить предупреждение (задача в чате %s)", chat_id)

    # --- 2 и 3. Основные напоминания ---
    due_tasks = db.run_db(
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

            await core.bot.send_message(
                chat_id, message_text, reply_markup=ui.reminder_keyboard(task_id)
            )

            if next_time:
                # Двигаем задачу на следующее срабатывание и заново планируем предупреждения
                db.run_db(
                    "UPDATE tasks SET remind_at = ?, sent = 0 WHERE id = ?",
                    (int(next_time.timestamp()), task_id),
                )
                db.plan_advances(task_id, next_time, db.parse_advance(advance_min), now)
            else:
                db.run_db("UPDATE tasks SET sent = 1 WHERE id = ?", (task_id,))
        except Exception:
            log.exception("Не удалось отправить напоминание №%s", task_id)


async def send_digests(now):
    """Утренняя сводка: в назначенное время присылает задачи на сегодня."""
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    users = db.run_db(
        "SELECT chat_id, digest_time FROM users WHERE digest_on = 1 AND last_digest != ?",
        (today,),
        fetch=True,
    )
    for chat_id, digest_time in users:
        if hhmm < digest_time:
            continue
        # Сначала помечаем, что сводка за сегодня обработана, чтобы не прислать дважды
        db.run_db("UPDATE users SET last_digest = ? WHERE chat_id = ?", (today, chat_id))
        body = ui.render_today(chat_id, now)
        if body is None:
            continue  # задач на сегодня нет — не беспокоим
        try:
            await core.bot.send_message(
                chat_id,
                f"🌅 Доброе утро! Задачи на сегодня "
                f"({WD_SHORT[now.weekday()]} {now.strftime('%d.%m')}):\n\n{body}",
            )
        except Exception:
            log.exception("Не удалось отправить сводку (чат %s)", chat_id)


async def reminder_loop():
    """
    Работает в фоне, пока запущен бот. Каждые 20 секунд заглядывает в базу:
    не пора ли что-то напомнить или прислать сводку.
    """
    while True:
        try:
            now = core.now_local()
            await process_reminders(now)
            await send_digests(now)
            db.cleanup(now)
        except Exception:
            # Если что-то пошло не так (например, нет интернета), бот не падает,
            # а пишет полную ошибку в журнал и пробует снова через 20 секунд
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(20)
