"""目标邮箱只是本机资料；必须与官方授权身份相符才能用于执行。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.account_service import AccountService
from codex_workbench.service import Workbench
from codex_workbench.store import Store
from tests.fakes import MemoryCredentials


class IdentityRpc:
    identity = None
    calls = []
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def request(self, method, params=None):
        self.calls.append(method)
        if method == 'account/read': return {'account':self.identity}
        if method == 'account/rateLimits/read':
            return {'accountId':'fixture-subject','rateLimits':{'limitId':'codex','primary':{'usedPercent':20,'windowDurationMins':10080}}}
        raise AssertionError(method)


class AccountIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.data=self.root/'data';self.data.mkdir();self.home=self.root/'account';self.home.mkdir(mode=0o700)
        self.store=Store(self.data/'workbench.sqlite3')
        self.account=self.store.create_execution_account('数字占位',str(self.home))
        self.store.rename_execution_account(self.account['id'],'目标账户','person@example.invalid')
        self.accounts=AccountService(self.store,self.data/'accounts','unused',self.root)
        IdentityRpc.identity=None;IdentityRpc.calls=[]
    def tearDown(self):
        self.accounts.close();self.temp.cleanup()

    def test_expected_email_does_not_claim_login_and_wrong_identity_is_rejected(self):
        with patch('codex_workbench.account_service.AccountRpc',IdentityRpc):
            value=self.accounts.status(self.account['id'])
            self.assertEqual('not_logged_in',value['login']['status'])
            self.assertEqual('person@example.invalid',value['account']['expected_email'])
            IdentityRpc.identity={'type':'chatgpt','email':'other@example.invalid','planType':'pro'}
            value=self.accounts.status(self.account['id'])
            self.assertEqual('identity_mismatch',value['login']['status'])
            self.assertIsNone(value['usage'])
            self.assertNotIn('account/rateLimits/read',IdentityRpc.calls)
            IdentityRpc.identity={'type':'chatgpt','email':'person@example.invalid','planType':'pro'}
            self.assertEqual('ready',self.accounts.status(self.account['id'])['login']['status'])

    def test_readonly_service_cannot_rename_or_export_target_email(self):
        from readonly_fixture import Fixture
        fixture=Fixture();self.addCleanup(fixture.close)
        with self.assertRaises(ValueError):fixture.board.call('account_rename',{'id':'current','name':'new','expected_email':'person@example.invalid'})
        fixture.board.call('workbench_state',{'view':'accounts'})
        self.assertFalse((fixture.root/'resources').exists())
