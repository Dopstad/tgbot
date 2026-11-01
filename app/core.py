"""
Ядро: общие настройки и «живые» объекты, которые нужны всем остальным файлам.
Здесь: токен, часовой пояс, сам бот и диспетчер, текущее время.
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Говорим программе, что живём по времени Екатеринбурга, даже если сервер
# стоит в другой стране. Без этого слова вроде «завтра» могут считаться
# от времени сервера, а не твоего.
os.environ["TZ"] = "Asia/Yekaterinburg"
if hasattr(time, "tzset"):
    time.tzset()

from aiogram import Bot, Dispatcher  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

# Твой часовой пояс: Екатеринбург (UTC+5)
TZ = ZoneInfo("Asia/Yekaterinburg")



def db_path_from_env(environ):
    """
    Где лежит файл базы с задачами.
    На хостинге Bothost постоянное хранилище (переживает пересборку) находится в /app/data,
    поэтому там в настройках бота задаётся переменная DATA_DIR=/app/data.
    Без переменной файл лежит рядом с bot.py (так удобно на Маке).
    """
    if environ.get("DB_PATH"):
        return environ["DB_PATH"]
    return os.path.join(environ.get("DATA_DIR", "."), "tasks.db")


DB_PATH = db_path_from_env(os.environ)

# Во сколько присылать утреннюю сводку по умолчанию
DEFAULT_DIGEST_TIME = "08:00"

bot = Bot(token=TOKEN)
dp = Dispatcher()


def now_local():
    """Текущее время в Екатеринбурге. Все остальные файлы берут время только отсюда."""
    return datetime.now(TZ)
