"""真实 HTTP → 工作台 → 隔离探针 → 回环模型的手动验证闭环。"""
import http.client
import json
import tempfile
import threading
import time
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from codex_workbench.api import PreviewServer
from codex_workbench.model_registry import ModelRegistry
from codex_workbench.service import Workbench
from tests.browser_fixture import FixtureModel
from tests.test_service import FakeAccounts
from tests.fakes import MemoryCredentials


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_http_rejects_model_create_and_validate(self):
        import http.client,json,threading
        from codex_workbench.api import PreviewServer
        server=PreviewServer(('127.0.0.1',0),self.board);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for name in ['model_create','model_update','model_validate']:
                c=http.client.HTTPConnection('127.0.0.1',server.server_port)
                c.request('POST','/rpc',json.dumps({'name':name,'arguments':{}}),{'Content-Type':'application/json','X-Workbench-Request':'1'})
                result=json.loads(c.getresponse().read());c.close();self.assertTrue(result['isError'])
        finally:server.shutdown();server.server_close();thread.join(2)
        self.assertEqual([],self.fixture.secret_reads)



if __name__ == "__main__":
    unittest.main()
