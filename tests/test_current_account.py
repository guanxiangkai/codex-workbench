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
                self.assertEqual('用户名未提供',result[0]['name'])
                self.assertEqual('unavailable',result[0]['name_source'])
                self.assertEqual('sample@example.invalid',result[0]['email'])
                with patch.object(reader,'identity',return_value={'type':'chatgpt','email':'sample@example.invalid','displayName':'示例姓名','planType':'pro'}):
                    self.assertEqual('用户名未提供',reader.accounts()[0]['name'])
                    named=reader.refresh_profile('current')
                self.assertEqual('示例姓名',named['name']);self.assertEqual('official',named['name_source'])
                self.assertEqual('示例姓名', reader.accounts(cached_only=True)[0]['name'])

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
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT,subject_id TEXT)')
                db.executemany('INSERT INTO execution_accounts VALUES(?,?,?)',[('current','Current','subject-a'),('other','Other','subject-a')])
                db.execute('CREATE TABLE preferences(singleton INTEGER,default_execution_account_id TEXT)')
                db.execute("INSERT INTO preferences VALUES(1,'other')")
            before=database.read_bytes()
            # No real auth data; both account records use the same synthetic RPC context.
            with patch('codex_workbench.readonly_sources.current_account_home',return_value=str(root)),patch('codex_workbench.readonly_sources.validate_account_home',return_value=str(root)),patch.object(NativeRead,'rpc',return_value=FakeRpc(current=True)):
                result=NativeRead(database,'unused',catalog=object()).accounts()
                self.assertFalse(result[0]['is_default']);self.assertTrue(result[0]['is_current'])
                self.assertTrue(result[1]['is_default']);self.assertFalse(result[1]['is_current'])
            self.assertEqual(before,database.read_bytes())

    def test_unconfirmed_legacy_account_is_not_listed(self):
        import sqlite3
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);database=root/'accounts.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT,codex_home TEXT,subject_id TEXT)')
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?,NULL)',('other','Other',str(root)))
            with patch('codex_workbench.readonly_sources.validate_account_home',return_value=str(root)),patch.object(NativeRead,'rpc',return_value=FakeRpc(current=True)):
                accounts=NativeRead(database,'unused',catalog=object()).accounts()
            self.assertEqual(['current'], [account['id'] for account in accounts])

    def test_targeted_profile_refresh_only_opens_requested_account_rpc(self):
        import sqlite3
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); database = root / 'accounts.sqlite'; other_home = root / 'other'; other_home.mkdir()
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT,display_name TEXT,expected_email TEXT,codex_home TEXT,kind TEXT,subject_id TEXT)')
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?,?,?,?,?)',
                           ('other', 'Other', None, 'other@example.invalid', str(other_home), 'isolated', 'subject-other'))
                db.execute('CREATE TABLE preferences(singleton INTEGER,default_execution_account_id TEXT)')
            opened = []
            class OtherRpc:
                def __init__(self, _codex, home, _lease, *, current, timeout):
                    opened.append((home, current, timeout))
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def request(self, method, _params=None):
                    if method == 'account/read':
                        return {'account': {'type': 'chatgpt', 'email': 'other@example.invalid', 'name': 'Other'}}
                    if method == 'account/rateLimits/read':
                        return {'accountId': 'subject-other', 'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 20, 'windowDurationMins': 10080}}}
                    raise AssertionError(method)
            with patch('codex_workbench.readonly_sources.current_account_home', return_value=str(root)), \
                 patch('codex_workbench.readonly_sources.validate_account_home', return_value=str(other_home)):
                account = NativeRead(database, 'unused', rpc_factory=OtherRpc, catalog=object()).refresh_profile('other')
            self.assertEqual('other', account['id'])
            self.assertNotIn('subject_binding', account['profile_status'])
            self.assertEqual([(str(other_home), False, 15)], opened)

    def test_cached_current_profile_requires_matching_observed_subject(self):
        import sqlite3
        from codex_workbench.account_snapshot import AccountSnapshot
        from codex_workbench.readonly_sources import NativeRead, _subject_binding
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); database = root / 'accounts.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT,subject_id TEXT)')
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?)', ('current', 'Current', 'subject-new'))
            account = {'id': 'current', 'subject_id': 'subject-new'}
            snapshots = AccountSnapshot(root)
            snapshots.save_profile(account, {'name': 'Old profile', 'name_source': 'official',
                                             'profile_status': {'source': 'official_rpc', 'subject_binding': _subject_binding('subject-old')}})
            reader = NativeRead(database, 'unused', catalog=object())
            stale = reader.accounts(cached_only=True)[0]
            self.assertEqual('用户名未提供', stale['name'])
            self.assertNotIn('profile_status', stale)
            snapshots.save_profile(account, {'name': 'New profile', 'name_source': 'official',
                                             'profile_status': {'source': 'official_rpc', 'subject_binding': _subject_binding('subject-new')}})
            matched = reader.accounts(cached_only=True)[0]
            self.assertEqual('New profile', matched['name'])
            self.assertNotIn('subject_binding', matched['profile_status'])

    def test_live_subject_check_binds_a_legacy_current_profile_before_future_cache_reads(self):
        import sqlite3
        from codex_workbench.account_snapshot import AccountSnapshot
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); database = root / 'accounts.sqlite'; native = root / 'official'; native.mkdir()
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE execution_accounts(id TEXT,name TEXT,subject_id TEXT)')
                db.execute('INSERT INTO execution_accounts VALUES(?,?,?)', ('current', 'Current', 'subject-a'))
            account = {'id': 'current', 'subject_id': 'subject-a'}
            AccountSnapshot(root).save_profile(account, {'name': 'Legacy profile', 'name_source': 'official'})
            with patch('codex_workbench.readonly_sources.current_account_home', return_value=str(native)):
                reader = NativeRead(database, 'unused', rpc_factory=FakeRpc, catalog=object())
                self.assertEqual('Legacy profile', reader.accounts()[0]['name'])
                self.assertEqual('Legacy profile', reader.accounts(cached_only=True)[0]['name'])
                self.assertIsNotNone(reader.profile_subject_binding('current'))

    def test_subjectless_current_fallback_reuses_profile_only_after_matching_usage_check(self):
        from codex_workbench.account_snapshot import AccountSnapshot
        from codex_workbench.readonly_sources import NativeRead, _subject_binding
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); database = root / 'accounts.sqlite'
            account = {'id': 'current'}
            snapshots = AccountSnapshot(root)
            snapshots.save_profile(account, {'name': 'Confirmed fallback', 'name_source': 'official',
                                             'profile_status': {'source': 'official_rpc', 'subject_binding': _subject_binding('subject-a')}})
            snapshots.save_usage(account, {'remaining_percent': 80}, subject='subject-a')
            reader = NativeRead(database, 'unused', catalog=object())
            self.assertEqual('Confirmed fallback', reader.accounts(cached_only=True)[0]['name'])
            self.assertEqual(_subject_binding('subject-a'), reader.profile_subject_binding('current'))
            snapshots.save_usage(account, {'remaining_percent': 20}, subject='subject-b')
            stale = reader.accounts(cached_only=True)[0]
            self.assertEqual('用户名未提供', stale['name'])
            self.assertNotIn('profile_status', stale)
            self.assertIsNone(reader.profile_subject_binding('current'))

    def test_online_subject_switch_removes_previously_projected_fallback_profile(self):
        from codex_workbench.readonly_sources import NativeRead
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); native = root / 'official'; native.mkdir()
            class SwitchingRpc:
                subject = 'subject-a'
                def __init__(self, *_args, **_kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def request(self, method, _params=None):
                    if method == 'account/read':
                        return {'account': {'type': 'chatgpt', 'email': 'sample@example.invalid',
                                            'displayName': 'Subject A', 'planType': 'pro'}}
                    if method == 'account/rateLimits/read':
                        return {'accountId': self.subject, 'rateLimits': {
                            'limitId': 'codex', 'primary': {'usedPercent': 20, 'windowDurationMins': 10080}}}
                    raise AssertionError(method)
            with patch('codex_workbench.readonly_sources.current_account_home', return_value=str(native)):
                reader = NativeRead(root / 'absent.sqlite', 'unused', rpc_factory=SwitchingRpc, catalog=object())
                self.assertEqual('Subject A', reader.refresh_profile('current')['name'])
                SwitchingRpc.subject = 'subject-b'
                switched = reader.accounts()[0]
                self.assertEqual('用户名未提供', switched['name'])
                self.assertEqual('unavailable', switched['name_source'])
                self.assertNotIn('profile_status', switched)
                self.assertEqual('用户名未提供', reader.accounts(cached_only=True)[0]['name'])
