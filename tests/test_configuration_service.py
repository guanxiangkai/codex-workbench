from readonly_fixture import Fixture as ReadonlyFixture
"""已退役配置中心的服务级契约。"""
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
    def test_configuration_view_and_tools_are_retired(self):
        from codex_workbench.catalog import TOOLS
        self.assertEqual(['open_workbench'],[t['name'] for t in TOOLS if t.get('_meta',{}).get('openai/ui')])
        with self.assertRaises(ValueError):self.board.call('workbench_state',{'view':'config'})
        for name in ('credential_list','credential_details','service_list','service_detail'):
            with self.subTest(name=name), self.assertRaises(ValueError):self.board.call(name,{})
        self.assertEqual([],self.fixture.secret_reads)
