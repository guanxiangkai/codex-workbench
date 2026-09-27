import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_workbench.account_snapshot import AccountSnapshot
from codex_workbench.readonly_sources import NativeRead


class AccountSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'workbench.sqlite3'
        self.account={'id':'current','codex_home':str(self.root),'subject_id':'subject-a','expected_email':'a@example.invalid'}
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE execution_accounts(id TEXT,codex_home TEXT,subject_id TEXT,expected_email TEXT)')
            db.execute('INSERT INTO execution_accounts VALUES(:id,:codex_home,:subject_id,:expected_email)',self.account)
        self.cache=AccountSnapshot(self.root)
        self.item={'email':'a@example.invalid','name':'Alice','name_source':'official','plan':'pro','remaining_percent':80,'reset_cards':2,'resets_at':2000000000,'observed_at':'2026-01-01T00:00:00+00:00'}
        self.cache.save_profile(self.account,self.item);self.cache.save_usage(self.account,self.item)
        self.reader=NativeRead(self.db,'unused',catalog=object())

    def test_offline_start_and_failed_refresh_preserve_initial_data(self):
        with patch.object(self.reader,'rpc',side_effect=ValueError('offline')) as rpc:
            cached=self.reader.accounts(cached_only=True)[0];rpc.assert_not_called()
            failed=self.reader.accounts()[0]
        for item in (cached,failed):
            self.assertEqual('Alice',item['name']);self.assertEqual(80,item['remaining_percent'])
            self.assertEqual(self.item['observed_at'],item['observed_at'])
        self.assertEqual('failed',failed['usage_refresh']['state'])
        # A restart still reads the separate JSON files without a network call.
        self.assertEqual('a@example.invalid',NativeRead(self.db,'unused',catalog=object()).accounts(cached_only=True)[0]['email'])

    def test_usage_only_refresh_does_not_rewrite_profile_and_zero_is_valid(self):
        path=self.cache.path(self.account,'profile');before=path.read_bytes();stamp=path.stat().st_mtime_ns
        calls=[]
        class Rpc:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,method,*args):
                calls.append(method)
                return {'accountId':'subject-a','rateLimits':{'limitId':'codex','primary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':2000000001}}}
        with patch.object(self.reader,'rpc',return_value=Rpc()):result=self.reader.accounts()[0]
        self.assertEqual(['account/rateLimits/read'],calls)
        self.assertEqual(0,result['remaining_percent']);self.assertEqual('Alice',result['name'])
        self.assertEqual(before,path.read_bytes());self.assertEqual(stamp,path.stat().st_mtime_ns)

    def test_changed_subject_invalidates_old_data(self):
        class Rpc:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,*args):return {'accountId':'different'}
        with patch.object(self.reader,'rpc',return_value=Rpc()):result=self.reader.accounts()[0]
        self.assertEqual('identity_mismatch',result['login_status']);self.assertIsNone(result.get('email'))
        self.assertFalse(self.cache.cached(self.account))

    def test_registry_change_never_reuses_old_binding(self):
        changed={**self.account,'subject_id':'different'}
        self.assertEqual({},self.cache.cached(changed))

    def test_partial_usage_preserves_missing_fields(self):
        self.cache.save_usage(self.account,{'remaining_percent':0,'reset_cards':None})
        self.assertEqual(0,self.cache.cached(self.account)['remaining_percent'])
        self.assertEqual(2,self.cache.cached(self.account)['reset_cards'])

if __name__=='__main__':unittest.main()
