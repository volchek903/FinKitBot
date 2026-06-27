import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TrialDurationStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "bot.sqlite3"

        config_stub = types.ModuleType("app.config")
        config_stub.get_settings = lambda: types.SimpleNamespace(database_file=self.db_path)
        sys.modules["app.config"] = config_stub
        sys.modules.pop("app.storage", None)

        self.storage = importlib.import_module("app.storage")
        self.storage.init_db()

    def tearDown(self) -> None:
        sys.modules.pop("app.storage", None)
        self.tempdir.cleanup()

    def test_trial_duration_setting_overrides_default_hours(self) -> None:
        self.assertEqual(self.storage.get_trial_duration_hours(24), 24)

        updated_users = self.storage.set_trial_duration_days(5)

        self.assertEqual(updated_users, 0)
        self.assertEqual(self.storage.get_trial_duration_days(24), 5)
        self.assertEqual(self.storage.get_trial_duration_hours(24), 120)

    def test_trial_duration_change_recalculates_existing_subscribers(self) -> None:
        activation = self.storage.activate_trial(
            user_id=101,
            chat_id=101,
            username="admin-test",
            first_name="Admin",
            duration_hours=24,
        )
        subscriber = activation["subscriber"]
        started_at = datetime.fromisoformat(subscriber["started_at"])
        expires_at = datetime.fromisoformat(subscriber["expires_at"])
        self.assertEqual(expires_at - started_at, timedelta(days=1))

        with self.storage._connect() as conn:
            conn.execute(
                "UPDATE subscribers SET trial_ended_notified_at = ? WHERE user_id = ?",
                (self.storage._now(), 101),
            )

        updated_users = self.storage.set_trial_duration_days(5)
        updated_subscriber = self.storage.get_subscriber(101)
        updated_started_at = datetime.fromisoformat(updated_subscriber["started_at"])
        updated_expires_at = datetime.fromisoformat(updated_subscriber["expires_at"])

        self.assertEqual(updated_users, 1)
        self.assertEqual(updated_started_at, started_at)
        self.assertEqual(updated_expires_at - updated_started_at, timedelta(days=5))
        self.assertIsNone(updated_subscriber["trial_ended_notified_at"])


if __name__ == "__main__":
    unittest.main()
