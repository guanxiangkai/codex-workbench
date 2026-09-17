"""工作台服务层的助手、账户与运行快照契约测试。"""

from __future__ import annotations

import json
import tempfile
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_workbench.service import Workbench
from codex_workbench.store import StoreError
from tests.fakes import MemoryCredentials


class FakeAccounts:
    """只模拟账户展示，不触发登录、执行或原生会话读取。"""

    def status(self, account_id: str, refresh: bool = True) -> dict:
        return {"account": {"id": account_id}, "usage": None,
                "login": {"status": "ready"}, "identity_id": "fake-subject"}

    def require_ready(self, account_id: str) -> None:
        pass

    def close(self) -> None:
        pass


class WorkbenchServiceTest(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_open_and_close_preserve_resources_and_old_data(self):
        import hashlib,sqlite3
        root=self.fixture.root;resource=root/'resources';resource.mkdir();rules=resource/'rules.md';rules.write_text('保留规则')
        db=root/'workbench.sqlite3'
        with sqlite3.connect(db) as c:c.execute('create table retained(id text,body text)');c.execute("insert into retained values('1','保留数据')")
        before=hashlib.sha256(db.read_bytes()).hexdigest()
        self.board.call('open_workbench',{});self.board.close()
        self.assertEqual(before,hashlib.sha256(db.read_bytes()).hexdigest());self.assertEqual('保留规则',rules.read_text())
    def test_no_settings_export_or_resource_initialization(self):
        self.board.call('workbench_state',{'view':'agents'})
        self.assertFalse((self.fixture.root/'resources').exists())
    def test_close_is_idempotent_and_has_no_validator(self):
        self.assertFalse(hasattr(self.board,'validator'));self.board.close();self.board.close()
        with self.assertRaises(ValueError):self.board.call('open_workbench',{})
    def test_no_rule_or_account_mutation_channels(self):
        for name in ['agent_update','agent_resource_write','account_rename','account_default']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.board.call(name,{})
    def test_views_are_lazy_and_do_not_read_unrelated_sources(self):
        self.board.call('workbench_state',{'view':'config'})
        self.assertEqual([],self.fixture.native.calls);self.assertEqual([],self.fixture.secret_reads)
    def test_no_executor_or_store_owned_by_view_service(self):
        for name in ['runner','executor','store','library','models']:self.assertFalse(hasattr(self.board,name))



if __name__ == "__main__":
    unittest.main()
