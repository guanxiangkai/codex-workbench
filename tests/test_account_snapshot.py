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
        self.cache.save_profile(self.account,{**self.item, 'profile_status': {
            'source': 'official_rpc',
            'subject_binding': __import__('hashlib').sha256(b'subject-a').hexdigest(),
        }})
        self.cache.save_usage(self.account,self.item,subject='subject-a')
        self.current_home=patch('codex_workbench.readonly_sources.current_account_home',return_value=str(self.root))
        self.current_home.start();self.addCleanup(self.current_home.stop)
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
                if method == 'account/read':
                    return {'account': {'type':'chatgpt','email':'a@example.invalid','planType':'pro'}}
                return {'accountId':'subject-a','rateLimits':{'limitId':'codex','primary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':2000000001}}}
        with patch.object(self.reader,'rpc',return_value=Rpc()):result=self.reader.accounts()[0]
        self.assertEqual(['account/read','account/rateLimits/read','account/read'],calls)
        self.assertEqual(0,result['remaining_percent']);self.assertEqual('Alice',result['name'])
        self.assertEqual(before,path.read_bytes());self.assertEqual(stamp,path.stat().st_mtime_ns)

    def test_repeated_login_status_preserves_profile_but_manual_update_changes_it(self):
        class Rpc:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,method,*args):
                if method == 'account/read':
                    return {'account': {'type':'chatgpt','email':'a@example.invalid','name':'Updated Alice','planType':'pro'}}
                return {'accountId':'subject-a','rateLimits':{'limitId':'codex','primary':{'usedPercent':25,'windowDurationMins':10080}}}
        path=self.cache.path(self.account,'profile');before=path.read_bytes()
        with patch.object(self.reader,'rpc',return_value=Rpc()):
            self.reader.refresh_profile('current',only_missing=True)
            self.assertEqual(before,path.read_bytes())
            result=self.reader.refresh_profile('current')
        self.assertEqual('Updated Alice',result['name'])
        self.assertNotEqual(before,path.read_bytes())

    def test_changed_subject_invalidates_old_data(self):
        class Rpc:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def request(self,method,*args):
                if method == 'account/read':
                    return {'account': {'type':'chatgpt','email':'a@example.invalid'}}
                return {'accountId':'different'}
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

    def test_usage_subject_switch_discards_omitted_old_usage_fields(self):
        self.cache.save_usage(self.account, {'remaining_percent': 25}, subject='subject-b')
        cached = self.cache.cached(self.account)
        self.assertEqual(25, cached['remaining_percent'])
        for field in ('reset_cards', 'resets_at', 'observed_at'):
            self.assertNotIn(field, cached)
        self.assertEqual(__import__('hashlib').sha256(b'subject-b').hexdigest(),
                         self.cache.usage_subject_binding(self.account))

    def test_historical_snapshot_migration_requires_profile_subject_evidence(self):
        account = {**self.account, 'id': 'migrated'}
        binding = __import__('hashlib').sha256(b'subject-a').hexdigest()
        historical = {'id': 'migrated', 'email': 'a@example.invalid', 'name': 'Historical', 'login_status': 'ready'}
        snapshots = {'contexts': {'old': {'accounts': {
            'updated_at': '2026-01-01T00:00:00+00:00', 'data': {'accounts': [historical]},
        }}}}
        (self.root / 'snapshots.json').write_text(json.dumps(snapshots))
        profiles = self.root / 'account-profiles.json'
        profiles.write_text(json.dumps({'version': 1, 'profiles': [{
            'view': 'accounts', 'identity': ['codex', 'migrated', 'a@example.invalid'], 'source': 'official_page',
        }]}))
        self.cache.migrate(account)
        self.assertFalse(self.cache.path(account, 'profile').exists())
        profiles.write_text(json.dumps({'version': 1, 'profiles': [{
            'view': 'accounts', 'identity': ['codex', 'migrated', 'a@example.invalid', binding], 'source': 'official_page',
        }]}))
        self.cache.migrate(account)
        self.assertEqual('Historical', self.cache.cached(account)['name'])

if __name__=='__main__':unittest.main()
