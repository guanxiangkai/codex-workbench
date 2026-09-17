"""受管认证边界测试；所有 JWT 均为合成数据，不使用真实登录材料。"""
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_workbench.gateway_auth import OfficialAuth
from codex_workbench.gateway_routes import GatewayError


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.path = self.home / 'auth.json'
        self.write()

    def write(self, subject='synthetic-subject', expiry=None):
        claims = {'exp': expiry or time.time()+3600, 'https://api.openai.com/auth': {'chatgpt_account_id': subject}}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        self.token = 'synthetic.' + encoded + '.synthetic'
        self.path.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': self.token,
                              'account_id': subject, 'refresh_token': 'synthetic-never-return'}}))
        self.path.chmod(0o600)

    def auth(self, rpc_factory=None):
        args = {} if rpc_factory is None else {'rpc_factory': rpc_factory}
        return OfficialAuth(self.home, 'synthetic-subject', 'unused-cli', **args)

    def test_exact_subject_snapshot_and_main_auth(self):
        auth = self.auth(); snapshot = auth.authorize()
        self.assertEqual(snapshot.subject_id, 'synthetic-subject')
        self.assertTrue(auth.accepts('Bearer '+self.token))
        self.assertFalse(auth.accepts('Bearer wrong'))
        self.assertNotIn(self.token, repr(snapshot))
        self.assertNotIn('synthetic-never-return', str(dict(snapshot.headers)))

    def test_wrong_subject_rejected_without_secret_error(self):
        self.write(subject='someone-else')
        with self.assertRaises(GatewayError) as caught: self.auth().authorize()
        self.assertNotIn(self.token, str(caught.exception))

    def test_insecure_auth_file_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(GatewayError): self.auth().authorize()

    def test_symlink_auth_rejected(self):
        auth = self.auth()
        actual = self.home / 'other'; self.path.rename(actual); self.path.symlink_to(actual)
        with self.assertRaises(GatewayError): auth.authorize()

    def test_hardlink_auth_rejected(self):
        auth = self.auth()
        os.link(self.path, self.home / 'other')
        with self.assertRaises(GatewayError): auth.authorize()

    def test_refresh_uses_official_rpc_once(self):
        self.write(expiry=time.time()+50)
        calls = []; owner = self
        class Rpc:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, method, params):
                calls.append((method, params)); owner.write()
                return {'account': {'type': 'chatgpt'}}
        self.auth(Rpc).authorize()
        self.assertEqual(calls, [('account/read', {'refreshToken': True})])

    def test_refresh_failure_has_no_fallback(self):
        self.write(expiry=time.time()+50)
        def failing(*args, **kwargs): raise RuntimeError('synthetic-secret')
        with self.assertRaises(GatewayError) as caught: self.auth(failing).authorize()
        self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_login_change_during_refresh_rejected(self):
        self.write(expiry=time.time()+50); owner = self
        class Rpc:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def request(self, *args):
                owner.write(subject='different-account')
                return {'account': {'type': 'chatgpt'}}
        with self.assertRaises(GatewayError): self.auth(Rpc).authorize()

    def test_ingress_does_not_refresh_near_expiry(self):
        self.write(expiry=time.time()+50)
        def forbidden(*args,**kwargs): raise AssertionError('入口不应触发续期')
        self.assertTrue(self.auth(forbidden).accepts('Bearer '+self.token))

    def test_verified_old_ingress_has_bounded_rotation_grace(self):
        auth=self.auth();old='Bearer '+self.token
        self.assertTrue(auth.accepts(old))
        self.write(expiry=time.time()+7200)
        self.assertTrue(auth.accepts(old))
        with patch('codex_workbench.gateway_auth.time.time', return_value=time.time()+61):
            self.assertFalse(auth.accepts(old))

    def test_grace_does_not_survive_logout_or_account_change(self):
        auth=self.auth();old='Bearer '+self.token;self.assertTrue(auth.accepts(old))
        self.write(subject='another-account')
        self.assertFalse(auth.accepts(old))
        self.write();self.assertTrue(auth.accepts('Bearer '+self.token))
        self.path.unlink();self.assertFalse(auth.accepts('Bearer '+self.token))


if __name__ == '__main__': unittest.main()
