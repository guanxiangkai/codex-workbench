"""网关更新门禁的直接回归；CLI 与探针为临时合成文件，进程调用以确定性结果替代。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_workbench.gateway_daemon as daemon
from codex_workbench.gateway_routes import GatewayError
from codex_workbench.native_compatibility import file_digest


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cli=self.root/'cli';self.cli.write_text('cli1')
        self.probe=self.root/'probe.py';self.probe.write_text('probe1')
        (self.root/'tests').mkdir();(self.root/'tests/native_protocol_probe.py').write_text('fixture1')

    def result(self,*args,**kwargs):
        source={name:file_digest(Path(daemon.__file__).parent/name) for name in ('gateway_routes.py','model_gateway.py')}
        probe=hashlib.sha256(self.probe.read_bytes()+(self.root/'tests/native_protocol_probe.py').read_bytes()+sys.version.encode()).hexdigest()
        return subprocess.CompletedProcess([],0,json.dumps({'passed':True,'cli_sha256':file_digest(self.cli),
                                           'source_sha256':source,'probe_sha256':probe}).encode())

    def check(self):return daemon.UpgradeCheck(self.root,self.cli,self.probe)

    def test_verified_cache_reused_across_restart(self):
        with patch.object(daemon.subprocess,'run',side_effect=self.result) as run:
            check=self.check();check();check();self.check()()
            self.assertEqual(run.call_count,1)

    def test_cli_change_requires_new_probe(self):
        with patch.object(daemon.subprocess,'run',side_effect=self.result) as run:
            check=self.check();check();self.cli.write_text('cli-new-version');check()
            self.assertEqual(run.call_count,2)

    def test_probe_change_invalidates_same_cli_cache(self):
        with patch.object(daemon.subprocess,'run',side_effect=self.result) as run:
            check=self.check();check();self.probe.write_text('probe2-additional-regressions');check()
            (self.root/'tests/native_protocol_probe.py').write_text('fixture2');check()
            self.assertEqual(run.call_count,3)

    def test_failure_never_marks_compatible(self):
        with patch.object(daemon.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'{}')):
            with self.assertRaises(GatewayError):self.check()()
        self.assertFalse((self.root/'compatibility.json').exists())
        self.assertFalse(json.loads((self.root/'status.json').read_text())['ready'])

    def test_cli_changed_during_probe_rejected(self):
        def racing(*args,**kwargs):
            result=self.result();self.cli.write_text('replaced-in-flight');return result
        with patch.object(daemon.subprocess,'run',side_effect=racing):
            with self.assertRaises(GatewayError):self.check()()
        self.assertFalse((self.root/'compatibility.json').exists())


if __name__=='__main__':unittest.main()
