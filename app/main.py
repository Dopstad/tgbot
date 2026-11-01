"""Сборка бота: подключаем все файлы с обработчиками и запускаем."""
import asyncio
import os

from app import core, db, scheduler
from app.handlers import basic, digest, menu, quick, reminders, tasks, wizard


def setup():
    """
    Порядок важен: Telegram отдаёт сообщение первому подходящему обработчику.
    Поэтому команды и кнопки идут раньше, мастера в середине, а обычные фразы последними.
    """
    core.dp.include_routers(
        basic.router, digest.router, tasks.router, wizard.router,
        menu.router, reminders.router, quick.router,
    )


def report_storage(existed_before):
    """
    Пишет в журнал, где лежит база и сколько в ней задач. Если после пересборки
    каждый раз видно «создана новая база», значит, файл не переживает пересборку.
    """
    path = os.path.abspath(core.DB_PATH)
    count = len(db.run_db("SELECT id FROM tasks WHERE sent = 0", fetch=True))
    state = f"найдена существующая, задач: {count}" if existed_before else "создана новая"
    print(f"[база] {path}: {state}", flush=True)
    if os.path.isdir("/app/data") and not path.startswith("/app/data/"):
        print(
            "[база] ВНИМАНИЕ: на сервере есть постоянная папка /app/data, но база лежит вне её. "
            "Добавь переменную окружения DATA_DIR=/app/data, иначе задачи пропадут при пересборке.",
            flush=True,
        )


async def main():
    existed_before = os.path.exists(core.DB_PATH)
    db.init_db()  # создаём базу, если её ещё нет
    report_storage(existed_before)
    setup()
    # Цикл напоминаний работает в фоне, параллельно с приёмом сообщений
    reminder_task = asyncio.create_task(scheduler.reminder_loop())
    try:
        await core.dp.start_polling(core.bot)
    finally:
        reminder_task.cancel()
