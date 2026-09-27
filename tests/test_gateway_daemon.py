"""网关持久化授权白名单的无账户回归。"""

import unittest

from codex_workbench.gateway_daemon import _authorized_accounts


class GatewayAuthorizationTests(unittest.TestCase):
    """守护进程只接受设置中显式且一致的账户声明。"""

    def settings(self):
        return {
            "authorized_account_ids": ["current", "synthetic-account"],
            "accounts": [
                {"id": "current", "home": "/tmp/current", "subject_id": "synthetic-current"},
                {"id": "synthetic-account", "home": "/tmp/other", "subject_id": "synthetic-other"},
            ],
        }

    def test_returns_only_explicitly_authorized_accounts_in_whitelist_order(self):
        accounts = _authorized_accounts(self.settings())
        self.assertEqual(["current", "synthetic-account"], [account["id"] for account in accounts])

    def test_rejects_missing_current_duplicates_and_declaration_mismatch(self):
        cases = []
        missing_current = self.settings(); missing_current["authorized_account_ids"] = ["synthetic-account"]
        cases.append(missing_current)
        duplicate = self.settings(); duplicate["authorized_account_ids"] = ["current", "current"]
        cases.append(duplicate)
        mismatch = self.settings(); mismatch["accounts"] = mismatch["accounts"][:1]
        cases.append(mismatch)
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, "授权账户配置无效"):
                    _authorized_accounts(settings)

class RegisteredAccountTests(unittest.TestCase):
    def test_old_session_ingress_can_follow_new_default_without_accepting_unknown_clients(self):
        from pathlib import Path
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from codex_workbench.gateway_daemon import RegisteredUpstreams
        from codex_workbench.model_gateway import Authorization
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root/'catalog.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,codex_home TEXT,subject_id TEXT)')
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?)', ('old', str(root/'accounts/old'), 'old-subject'))
            class Broker:
                def __init__(self, home, subject, cli, **kwargs): self.subject = subject
                def authorize(self): return Authorization(self.subject, {})
                def accepts(self, header): return header == 'Bearer synthetic-' + self.subject
            resolver = RegisteredUpstreams(path, 'synthetic-cli', [], broker_factory=Broker)
            with patch('codex_workbench.gateway_daemon.validate_account_home', side_effect=lambda p: p):
                self.assertTrue(resolver.accepts('Bearer synthetic-old-subject'))
                self.assertFalse(resolver.accepts('Bearer unregistered'))
                with sqlite3.connect(path) as db:
                    db.execute("DELETE FROM execution_accounts")
                self.assertFalse(resolver.accepts('Bearer synthetic-old-subject'))

    def test_new_registered_account_is_resolved_without_restart(self):
        from pathlib import Path
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from codex_workbench.gateway_daemon import RegisteredUpstreams
        from codex_workbench.gateway_routes import AccountTarget, GatewayError
        from codex_workbench.model_gateway import Authorization
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); db_path = root/'catalog.sqlite3'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,codex_home TEXT,subject_id TEXT)')
            created = []
            class Broker:
                def __init__(self, home, subject, cli, **kwargs):
                    created.append((home, subject)); self.subject = subject
                def authorize(self):
                    return Authorization(self.subject, {})
            resolver = RegisteredUpstreams(db_path, 'synthetic-cli', [], broker_factory=Broker)
            target = AccountTarget('added', 'new-subject')
            with self.assertRaises(GatewayError): resolver(target)
            with sqlite3.connect(db_path) as db:
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?)', ('added', str(root/'accounts'/'new'), 'new-subject'))
            with patch('codex_workbench.gateway_daemon.validate_account_home', return_value=str(root/'accounts'/'new')):
                upstream = resolver(target)
                self.assertEqual(upstream.authorize().subject_id, 'new-subject')
                self.assertIs(resolver(target), upstream)
                self.assertEqual(len(created), 1)
                with sqlite3.connect(db_path) as db:
                    db.execute("UPDATE execution_accounts SET subject_id='changed'")
                with self.assertRaises(GatewayError): resolver(target)

    def test_invalid_account_directory_is_rejected_before_broker_creation(self):
        from pathlib import Path
        import sqlite3
        import tempfile
        from unittest.mock import Mock
        from codex_workbench.gateway_daemon import RegisteredUpstreams
        from codex_workbench.gateway_routes import AccountTarget, GatewayError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'catalog.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,codex_home TEXT,subject_id TEXT)')
                db.execute("INSERT INTO execution_accounts VALUES('added','relative-home','subject')")
            factory = Mock()
            resolver = RegisteredUpstreams(path, 'synthetic-cli', [], broker_factory=factory)
            with self.assertRaises(GatewayError): resolver(AccountTarget('added','subject'))
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
