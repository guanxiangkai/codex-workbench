from readonly_fixture import Fixture as ReadonlyFixture
"""统一工作台入口与单条加密配置详情的服务级契约。"""
import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from codex_workbench.service import Workbench
from codex_workbench.catalog import BY_NAME,tools_for_page
from tests.fakes import MemoryCredentials
from tests.test_service import FakeAccounts

class ConfigurationServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_single_entry_and_no_automatic_secret_read(self):
        from codex_workbench.catalog import TOOLS
        self.assertEqual(['open_workbench'],[t['name'] for t in TOOLS if t.get('_meta',{}).get('openai/ui')])
        self.board.call('workbench_state',{'view':'config'});self.assertEqual([],self.fixture.secret_reads)
    def test_details_only_accept_public_request_and_return_ciphertext(self):
        result=self.board.call('credential_details',{'id':'item','public_key':'public','request_id':'request'})
        self.assertEqual('synthetic-envelope',result['ciphertext'])
        with self.assertRaises(ValueError):self.board.call('credential_details',{'id':'item','public_key':'p','request_id':'r','password':'synthetic'})
        self.assertEqual(1,len(self.fixture.secret_reads))
