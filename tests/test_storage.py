"""Проверка места хранения базы: она должна лежать в постоянной папке и не теряться."""
import os
import tempfile
import unittest

from app import core, db, main
from tests.harness import BotTestCase


class StoragePathTest(unittest.TestCase):
    def test_default_is_next_to_bot(self):
        self.assertEqual(core.db_path_from_env({}), os.path.join(".", "tasks.db"))

    def test_data_dir_variable(self):
        self.assertEqual(core.db_path_from_env({"DATA_DIR": "/app/data"}), "/app/data/tasks.db")

    def test_explicit_db_path_wins(self):
        self.assertEqual(core.db_path_from_env({"DATA_DIR": "/a", "DB_PATH": "/b/x.db"}), "/b/x.db")


class StorageTest(BotTestCase):
    async def test_missing_folder_is_created(self):
        core.DB_PATH = os.path.join(tempfile.mkdtemp(), "data", "nested", "tasks.db")
        db.init_db()
        self.assertTrue(os.path.exists(core.DB_PATH))

    async def test_tasks_survive_restart(self):
        from datetime import timedelta
        db.add_task(7, "Важное", self.now + timedelta(days=1), "", [])
        db.init_db()  # «перезапуск»: таблицы уже есть, данные не трогаем
        self.assertEqual([t[1] for t in db.get_tasks(7)], ["Важное"])

    async def test_startup_report(self):
        with self.assertLogs("app.main", level="INFO") as logs:
            main.report_storage(existed_before=True)
            main.report_storage(existed_before=False)
        joined = "\n".join(logs.output)
        self.assertIn("найдена существующая", joined)
        self.assertIn("создана новая", joined)

    async def test_versions_report(self):
        with self.assertLogs("app.main", level="INFO") as logs:
            main.report_versions()
        self.assertIn("aiogram==", logs.output[0])
