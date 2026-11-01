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

# Файл, где хранятся данные (создаётся сам рядом с bot.py)
DB_PATH = os.getenv("DB_PATH", "tasks.db")

# Во сколько присылать утреннюю сводку по умолчанию
DEFAULT_DIGEST_TIME = "08:00"

bot = Bot(token=TOKEN)
dp = Dispatcher()


def now_local():
    """Текущее время в Екатеринбурге. Все остальные файлы берут время только отсюда."""
    return datetime.now(TZ)
