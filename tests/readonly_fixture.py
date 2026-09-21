"""只读服务测试夹具：只有临时目录和合成原生/保险库数据。"""
import tempfile
from pathlib import Path
from unittest.mock import patch
from codex_workbench.service import Workbench
from test_readonly_service import FakeNative, FakeCredentials

class Fixture:
    def __init__(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.patch=patch('codex_workbench.service.UiRelease');self.release=self.patch.start().return_value;self.release.revision='test'
        self.native=FakeNative();self.credentials=FakeCredentials();self.secret_reads=[]
        def reader(*args):self.secret_reads.append(args[1:]);return {'ciphertext':'synthetic-envelope','entry_id':args[1]}
        versions=type('Versions',(),{'context':lambda self:'fixture-context'})()
        self.board=Workbench(self.root,self.root/'resources',native_reader=self.native,credential_catalog=self.credentials,credential_reader=reader,source_versions=versions)
    def close(self):
        self.board.close();self.patch.stop();self.temp.cleanup()
