"""
Тестовый «поддельный Telegram»: бот работает как обычно, но вместо настоящего Telegram
все его действия (отправить, удалить, изменить сообщение) записываются в журнал.
Так можно нажимать кнопки и писать сообщения прямо из тестов.
"""
import itertools
import os
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Message  # noqa: E402

from app import core, db, main, nav  # noqa: E402

main.setup()  # подключаем обработчики один раз


class MockSession(BaseSession):
    """Вместо отправки в Telegram запоминает, что бот пытался сделать."""

    def __init__(self):
        super().__init__()
        self.message_id = 100
        self.store = {}      # номер сообщения -> его текст и кнопки
        self.sent = []       # номера отправленных сообщений по порядку
        self.deleted = set()
        self.answers = []    # ответы на нажатия (всплывающие подсказки)

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        if name == "SendMessage":
            self.message_id += 1
            self.store[self.message_id] = {"text": data["text"], "markup": data.get("reply_markup")}
            self.sent.append(self.message_id)
            return Message.model_validate({
                "message_id": self.message_id, "date": 1730000000, "text": data["text"],
                "chat": {"id": data["chat_id"], "type": "private"},
            })
        if name == "DeleteMessage":
            self.deleted.add(data["message_id"])
        elif name == "EditMessageText":
            if data.get("message_id") in self.store:
                self.store[data["message_id"]] = {"text": data["text"], "markup": data.get("reply_markup")}
        elif name == "EditMessageReplyMarkup":
            if data.get("message_id") in self.store:
                self.store[data["message_id"]]["markup"] = data.get("reply_markup")
        elif name == "AnswerCallbackQuery":
            self.answers.append((data.get("text"), data.get("show_alert")))
        return True


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """Основа для тестов: чистая база, поддельный Telegram, управляемые часы."""

    CHAT = 7
    START = datetime(2026, 11, 2, 10, 0, tzinfo=core.TZ)  # понедельник, 10:00

    async def asyncSetUp(self):
        self._orig = (core.DB_PATH, core.bot, core.now_local)
        core.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
        self.session = MockSession()
        core.bot = Bot(token=os.environ["BOT_TOKEN"], session=self.session)
        self.now = self.START
        core.now_local = lambda: self.now
        nav.LAST_SCREEN.clear()
        core.dp.storage.storage.clear()
        db.init_db()
        self._ids = itertools.count(1)

    async def asyncTearDown(self):
        core.DB_PATH, core.bot, core.now_local = self._orig

    # ---------- действия пользователя ----------

    async def say(self, text):
        message = {
            "message_id": 5000 + next(self._ids), "date": 1730000000,
            "chat": {"id": self.CHAT, "type": "private"},
            "from": {"id": self.CHAT, "is_bot": False, "first_name": "E"}, "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await core.dp.feed_raw_update(core.bot, {"update_id": next(self._ids), "message": message})

    async def press(self, label=None, data=None):
        """Нажимает кнопку на последнем живом экране: по подписи или по callback_data."""
        message_id = self.screen_id()
        for button in self.buttons():
            if (label and button["text"] == label) or (data and button["callback_data"] == data):
                callback = {
                    "id": str(next(self._ids)), "chat_instance": "x", "data": button["callback_data"],
                    "from": {"id": self.CHAT, "is_bot": False, "first_name": "E"},
                    "message": {
                        "message_id": message_id, "date": 1730000000,
                        "chat": {"id": self.CHAT, "type": "private"},
                        "text": self.session.store[message_id]["text"],
                    },
                }
                await core.dp.feed_raw_update(
                    core.bot, {"update_id": next(self._ids), "callback_query": callback}
                )
                return
        raise AssertionError(f"Кнопки {label or data!r} нет. Есть: {[b['text'] for b in self.buttons()]}")

    # ---------- что видит пользователь ----------

    def live_screens(self):
        """Живые сообщения с кнопками под текстом."""
        result = []
        for message_id in self.session.sent:
            markup = self.session.store[message_id]["markup"]
            if message_id not in self.session.deleted and markup and "inline_keyboard" in markup:
                result.append(message_id)
        return result

    def screen_id(self):
        return self.live_screens()[-1]

    def screen_text(self):
        return self.session.store[self.screen_id()]["text"]

    def buttons(self):
        markup = self.session.store[self.screen_id()]["markup"]
        return [b for row in markup["inline_keyboard"] for b in row]

    def labels(self):
        return [b["text"] for b in self.buttons()]

    def last_text(self):
        """Текст последнего отправленного сообщения (любого)."""
        return self.session.store[self.session.sent[-1]]["text"]

    def tasks(self):
        return db.get_tasks(self.CHAT)
