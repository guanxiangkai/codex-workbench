import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.account_onboarding import AccountRegistry
from codex_workbench.account_service import AccountService
from codex_workbench.store import StoreError


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

    def test_current_status_registers_with_minimal_registry_and_preserves_default(self):
        registry = AccountRegistry(self.path)
        other = registry.create_execution_account("备用 Codex", str(self.home))
        registry.record_account_subject(other["id"], "other-subject")
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO preferences VALUES(1,?)", (other["id"],))
        current_home = self.root / "current"
        current_home.mkdir(mode=0o700)
        service = AccountService(registry, self.root / "accounts", "codex", current_home)
        identity = {"type": "chatgpt", "email": "a@example.invalid", "planType": "pro"}
        with patch('codex_workbench.account_onboarding.current_account_home', return_value=str(current_home)), \
             patch('codex_workbench.account_service.current_account_home', return_value=str(current_home)), \
             patch('codex_workbench.account_service.AccountRpc') as rpc, \
             patch.object(service, '_read_models', return_value=[]), \
             patch.object(service, '_read_defaults', return_value={}):
            rpc.return_value.__enter__.return_value.request.side_effect = lambda method, *args: (
                {"account": identity} if method == 'account/read' else {"accountId": "current-subject"})
            result = service.status('current')
            self.assertEqual('ready', result['login']['status'])
            self.assertEqual('current-subject', result['account']['subject_id'])
            changed = registry.register_current_account('当前 Codex 账户', str(current_home), 'changed-subject')
            self.assertEqual('changed-subject', changed['subject_id'])
            self.assertFalse(changed['isDefault'])
            with self.assertRaises(StoreError):
                registry.register_current_account('当前 Codex 账户', str(self.home), 'wrong-home')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(other['id'], db.execute('SELECT default_execution_account_id FROM preferences').fetchone()[0])
            self.assertEqual(2, db.execute('SELECT count(*) FROM execution_accounts').fetchone()[0])
            self.assertEqual({'execution_accounts', 'preferences'}, {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})


if __name__ == "__main__":
    unittest.main()
