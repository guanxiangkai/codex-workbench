"""模型 Key 的公开工具链、仓库优化记录和账户别名集成验证。"""
import json
import tempfile
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.service import Workbench
from tests.browser_fixture import FixtureAccounts
import tests.test_model_keys as encryption_fixture
from tests.fakes import MemoryCredentials


class MemoryVault:
    """只存放测试合成值，不触及真实保险库。"""

    def __init__(self):
        self.saved = []

    def store(self, key, context):
        self.saved.append((key, context))
        return "vault:synthetic-" + str(len(self.saved))


class KeyServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_plaintext_and_encrypted_model_writes_are_both_removed(self):
        for args in [{'key':'synthetic'},{'key_envelope':{}},{}]:
            with self.subTest(fields=list(args)),self.assertRaises(ValueError):self.board.call('model_create',args)
        self.assertEqual([],self.fixture.secret_reads)
    def test_listing_does_not_decrypt_or_reorganize(self):
        self.assertEqual([],self.board.call('model_list',{})['models']);self.assertEqual(0,self.fixture.credentials.calls)
    def test_account_alias_cannot_be_changed_by_read_tool(self):
        with self.assertRaises(ValueError):self.board.call('workbench_state',{'view':'accounts','name':'replacement'})
        with self.assertRaises(ValueError):self.board.call('account_rename',{})
    def test_resource_writes_and_directory_creation_are_not_exposed(self):
        with self.assertRaises(ValueError):self.board.call('agent_resource_write',{})
        self.assertFalse((self.fixture.root/'resources').exists())
    def test_model_reads_reject_version_or_key_parameters(self):
        for key in ['version','key','key_envelope']:
            with self.subTest(key=key),self.assertRaises(ValueError):self.board.call('model_detail',{'id':'m',key:1})



if __name__ == "__main__":
    unittest.main()
