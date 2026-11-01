"""Точка входа. Запуск: python3 bot.py. Вся работа бота лежит в папке app/."""
import asyncio

from app.main import main

if __name__ == "__main__":
    asyncio.run(main())
