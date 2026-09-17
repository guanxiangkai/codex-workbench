"""最终批次发现的类型、缓存及实际发送字节边界，全部使用合成数据。"""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from codex_workbench.appearance import validate_icon, AppearanceError
from codex_workbench.professional_services import validate_services
from codex_workbench.account_service import AccountService
from codex_workbench.codex_rpc import RpcError
from codex_workbench.credential_schema import probe_structure
from codex_workbench.capability_runtime import CapabilityRuntime
from codex_workbench.capability_manifest import CapabilityManifestError

class FinalEdges(unittest.TestCase):
    def test_unhashable_icon_kind_returns_validation_error(self):
        for kind in ([], {}):
            with self.subTest(kind=kind), self.assertRaises(AppearanceError):
                validate_icon({'kind':kind,'value':'synthetic'})

    def test_remote_ports_are_bounded(self):
        for port in ('0','65536','invalid'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_services([{'name':'fixture','base_url':'https://example.invalid:'+port+'/v1'}])

    def test_failed_refresh_does_not_resurrect_cached_login(self):
        service=object.__new__(AccountService)
        service.codex='unused';service.lease_fd=None
        service.current_cache={'time':time.monotonic(),'value':{'login':{'status':'ready'}}}
        with patch('codex_workbench.account_service.current_account_home',return_value='/synthetic'), patch('codex_workbench.account_service.AccountRpc',side_effect=RpcError('synthetic')):
            self.assertEqual('unavailable',service.current(refresh=True)['login']['status'])
            self.assertEqual('unavailable',service.current()['login']['status'])
        self.assertIsNone(service.current_cache)

    def test_openssh_key_structure_without_exposing_key(self):
        value=Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.OpenSSH,serialization.NoEncryption())
        result=probe_structure(value)
        self.assertEqual(('indexed','private_key',['private_key']),(result['structure_status'],result['kind'],result['has_fields']))
        self.assertNotIn(value.decode(),str(result))

    def test_final_media_bytes_must_match_selected_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'image.png';raw=b'\x89PNG\r\n\x1a\noriginal';path.write_bytes(raw)
            manifest={'resource_snapshots':[{'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest()}]}
            runtime=object.__new__(CapabilityRuntime)
            with patch('codex_workbench.capability_runtime._read_regular',return_value=(b'\x89PNG\r\n\x1a\nchanged','image/png','.png')), self.assertRaises(CapabilityManifestError):
                runtime._prepare_request('multimodal_image',{'image_path':str(path)},manifest)

    def test_legacy_token_secret_and_host_are_explicit_fields(self):
        for payload,kind,fields in [(b'{"access_token":"synthetic"}','token',['token']), (b'{"secret":"synthetic","hostname":"example.invalid"}','secret',['host','secret'])]:
            with self.subTest(kind=kind):
                result=probe_structure(payload)
                self.assertEqual(('indexed',kind,fields),(result['structure_status'],result['kind'],result['has_fields']))
                self.assertNotIn('synthetic',str(result))
