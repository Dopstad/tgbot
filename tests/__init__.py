"""Тесты бота. Запуск: python3 -m unittest discover -s tests -t . -v"""
import os

# Токен для тестов ненастоящий: тесты не выходят в интернет и ничего не отправляют в Telegram
os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
