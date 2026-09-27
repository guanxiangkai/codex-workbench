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
    def test_failed_reset_analysis_retains_shared_history_metadata(self):
        previous={'scope':'official_manual_reset','observed_at':'2026-09-27T07:00:00Z','summary':'last success'}
        retained=dict(previous,signal='unknown',stale=True,error='analyzer_unavailable',last_success_at=previous['observed_at'],history_summary={'reported_count':55,'tracked_count':1})
        self.board._scheduled_accounts=[{'id':'a','remaining_percent':70,'reset_analysis':previous}]
        self.board.reset_analyzer=Mock()
        self.board.reset_analyzer.cached.return_value=retained
        account=self.board._collect_state('accounts')['accounts'][0]
        self.assertEqual(70,account['remaining_percent'])
        self.assertEqual(retained,account['reset_analysis'])
        self.board.reset_analyzer.force_refresh.assert_not_called()

    def test_manual_reset_job_publishes_cache_and_exposes_failure_without_quota_rpc(self):
        from codex_workbench.snapshot_store import SnapshotScheduler
        self.board.native=Mock()
        self.board.native.accounts.return_value=[{'id':'a','remaining_percent':70}]
        self.board.reset_analyzer=Mock()
        self.board.collect_snapshot=Mock(return_value=True)
        scheduler=SnapshotScheduler(self.board)
        self.addCleanup(scheduler.close)
        good={'scope':'official_manual_reset','signal':'none','observed_at':'2026-09-27T08:00:00Z'}
        failed=dict(good,error='analyzer_unavailable',stale=True,last_success_at=good['observed_at'])
        for analysis, expected in ((good,'ready'),(failed,'error')):
            self.board.reset_analyzer.force_refresh.return_value=analysis
            scheduler._refresh_provider('manual_reset')
            task=next(t for t in scheduler.provider_tasks() if t['id']=='manual_reset')
            self.assertEqual(expected,task['state'])
            self.assertEqual(expected=='error',bool(task['error']))
            self.assertIsNotNone(task['last_completed_at'])
            self.assertEqual(analysis,self.board._scheduled_accounts[0]['reset_analysis'])
        self.assertEqual(2,self.board.collect_snapshot.call_count)
        self.assertTrue(all(c.kwargs=={'cached_only':True} for c in self.board.native.accounts.call_args_list))

    def test_sync_queues_manual_reset_without_refreshing_account_view(self):
        self.board.snapshot_scheduler=Mock()
        self.board.state=Mock(return_value={'accounts':[]})
        self.board.sync('accounts',provider='manual_reset',refresh=True)
        self.board.snapshot_scheduler.request_provider.assert_called_once_with('manual_reset')
        self.board.snapshot_scheduler.touch.assert_called_once_with('accounts',refresh=False)



if __name__ == "__main__":
    unittest.main()
