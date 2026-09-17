"""通用凭证的端到端加密边界，仅使用本机合成保险库替身。"""
import json
import shlex
import tempfile
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from pathlib import Path
from codex_workbench.credentials import CredentialCatalog
from codex_workbench.service import Workbench
from tests.browser_fixture import FixtureAccounts, FixturePayloadVault
import tests.test_model_keys as encryption_fixture

class CredentialServiceTest(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_credential_creation_and_metadata_changes_rejected(self):
        for name in ['credential_create','credential_update','credential_folder_create','credential_folder_update','credential_key_prepare']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.board.call(name,{})
        self.assertEqual([],self.fixture.secret_reads);self.assertEqual([],list(self.fixture.root.iterdir()))
    def test_list_never_decrypts_and_explicit_details_are_encrypted(self):
        self.board.call('credential_list',{});self.assertEqual([],self.fixture.secret_reads)
        result=self.board.call('credential_details',{'id':'item','public_key':'public','request_id':'request'})
        self.assertNotIn('password',result);self.assertIn('ciphertext',result)
