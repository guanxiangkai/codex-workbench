"""当前账户读取与隔离环境的回归，不进行真实登录或模型调用。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.account_service import AccountService
from codex_workbench.service import Workbench
from codex_workbench.store import Store
from codex_workbench.account_runtime import account_environment
from tests.fakes import MemoryCredentials


class FakeRpc:
    subject = "subject-a"
    calls = []

    def __init__(self, *args, **kwargs):
        assert kwargs.get("current") is True

    def request(self, method, params=None):
        self.calls.append(method)
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "sample@example.invalid", "planType": "pro"}}
        if method == "account/rateLimits/read":
            return {"accountId": self.subject, "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 10080}}}
        if method == "model/list":
            return {"data": [{"id": "test-model", "model": "test-model", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "medium"}], "defaultReasoningEffort": "medium"}]}
        if method == "config/read":
            return {"config": {"model": "test-model", "model_reasoning_effort": "medium"}}
        raise AssertionError("当前账户不能登录或执行其他操作")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class CurrentAccountTests(unittest.TestCase):
    def test_current_account_read_does_not_register_a_default(self):
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);native=root/'official';native.mkdir()
            with patch('codex_workbench.readonly_sources.current_account_home',return_value=str(native)):
                reader=NativeRead(root/'absent.sqlite','unused',rpc_factory=FakeRpc,catalog=object())
                result=reader.accounts()
                self.assertEqual('ready',result[0]['login_status']);self.assertEqual(80,result[0]['remaining_percent'])
                self.assertFalse((root/'absent.sqlite').exists());self.assertNotIn('isDefault',result[0])

    def test_current_environment_does_not_inherit_another_home_or_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            current = str(Path(temporary).resolve())
            with patch("codex_workbench.account_runtime.current_account_home", return_value=current):
                result = account_environment(current, {"CODEX_HOME": "/another", "OPENAI_API_KEY": "synthetic-key"}, use_current=True)
                self.assertEqual(current, result["CODEX_HOME"])
                self.assertNotIn("OPENAI_API_KEY", result)
                with self.assertRaises(ValueError):
                    account_environment("/another", use_current=True)

    def test_registered_default_is_independent_of_current_login(self):
        import sqlite3
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);database=root/'accounts.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT)')
                db.executemany('INSERT INTO execution_accounts VALUES(?,?)',[('current','Current'),('other','Other')])
                db.execute('CREATE TABLE preferences(singleton INTEGER,default_execution_account_id TEXT)')
                db.execute("INSERT INTO preferences VALUES(1,'other')")
            before=database.read_bytes()
            # No real auth data; both account records use the same synthetic RPC context.
            with patch('codex_workbench.readonly_sources.current_account_home',return_value=str(root)),patch('codex_workbench.readonly_sources.validate_account_home',return_value=str(root)),patch.object(NativeRead,'rpc',return_value=FakeRpc(current=True)):
                result=NativeRead(database,'unused',catalog=object()).accounts()
                self.assertFalse(result[0]['is_default']);self.assertTrue(result[0]['is_current'])
                self.assertTrue(result[1]['is_default']);self.assertFalse(result[1]['is_current'])
            self.assertEqual(before,database.read_bytes())
