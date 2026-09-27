import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_workbench.account_onboarding import AccountRegistry


class AccountOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "accounts" / "account-a"
        self.home.mkdir(parents=True, mode=0o700)
        self.home.chmod(0o700)
        self.path = self.root / "workbench.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_registers_only_account_tables_and_never_sets_default(self):
        registry = AccountRegistry(self.path)
        account = registry.create_execution_account("备用 Codex", str(self.home))
        self.assertFalse(account["isDefault"])
        self.assertIsNone(account["subject_id"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM execution_accounts").fetchone()[0])
        self.assertEqual([], AccountRegistry(self.path).list_execution_accounts())
        with sqlite3.connect(self.path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"execution_accounts", "preferences"}, tables)

    def test_confirmed_subject_is_persisted_for_default_account_validation(self):
        registry = AccountRegistry(self.path)
        account = registry.create_execution_account("备用 Codex", str(self.home))
        confirmed = registry.record_account_subject(account["id"], "official-subject")
        self.assertEqual("official-subject", confirmed["subject_id"])
        self.assertEqual(account["id"], registry.list_execution_accounts()[0]["id"])
        self.assertEqual(account["id"], AccountRegistry(self.path).list_execution_accounts()[0]["id"])
        self.assertEqual({}, registry.pending)


if __name__ == "__main__":
    unittest.main()
