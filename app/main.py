"""Сборка бота: подключаем все файлы с обработчиками и запускаем."""
import asyncio
import logging
import os
import sys
from importlib import metadata

from app import core, db, scheduler
from app.handlers import basic, digest, menu, quick, reminders, tasks, wizard

log = logging.getLogger(__name__)


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
    log.info("[база] %s: %s", path, state)
    if os.path.isdir("/app/data") and not path.startswith("/app/data/"):
        log.warning(
            "[база] ВНИМАНИЕ: на сервере есть постоянная папка /app/data, но база лежит вне её. "
            "Добавь переменную окружения DATA_DIR=/app/data, иначе задачи пропадут при пересборке."
        )


def report_versions():
    """Пишет в журнал версии библиотек: по ним потом фиксируем requirements.txt."""
    names = ["aiogram", "python-dotenv", "dateparser", "tzdata"]
    found = []
    for name in names:
        try:
            found.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            found.append(f"{name}==?")
    log.info("[версии] python==%s %s", sys.version.split()[0], " ".join(found))


def setup_logging():
    """Журнал: время, важность, где произошло. Идёт в обычный вывод, который показывает панель хостинга."""
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main():
    setup_logging()
    report_versions()
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
