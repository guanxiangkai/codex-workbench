"""网站身份登记不等同登录、来源匹配与名称持久化的回归检查。"""
import tempfile
import unittest
from pathlib import Path
from codex_workbench.website_accounts import WebsiteAccounts


class WebsiteAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.accounts = WebsiteAccounts(Path(self.temp.name) / 'accounts.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_registration_never_claims_login_and_duplicate_preserves_name(self):
        account = self.accounts.register('Fixture-User', '工作账户')
        self.assertEqual('unverified', account['verification_status'])
        self.assertIsNone(account['checked_at'])
        self.assertEqual(account, self.accounts.register('fixture-user', '覆盖名'))
        self.accounts.rename(account['id'], '新名称')
        observed = self.accounts.record_observation('fixture-user', 'https://github.com/fixture-user', 'chrome')
        self.assertEqual('新名称', observed['display_name'])
        self.assertEqual('browser_confirmed', observed['verification_status'])
        self.assertEqual(1, len(self.accounts.list()))
        self.assertNotIn('isDefault', observed)
        self.assertNotIn('codex_home', observed)

    def test_invalid_profile_or_secret_fields_cannot_be_used_as_identity(self):
        for name in ('https://github.com/user', 'bad--name', '-name', 'user/password', 'logout', 'settings'):
            with self.assertRaises(ValueError):
                self.accounts.register(name)
        with self.assertRaises(ValueError):
            self.accounts.record_observation('fixture', 'https://evil.invalid/fixture', 'chrome')
        with self.assertRaises(ValueError):
            self.accounts.record_observation('fixture', 'https://github.com/fixture', 'cookie')
        self.assertEqual([], self.accounts.list())
