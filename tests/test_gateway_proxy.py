"""合成代理设置验证；不修改系统代理，不访问真实账户或上游。"""
import unittest
import ssl
from unittest.mock import Mock, patch
from codex_workbench.gateway_routes import GatewayError
from codex_workbench.model_gateway import Authorization, Upstream, resolve_proxy

MODULE = 'codex_workbench.model_gateway.'

class ProxyTests(unittest.TestCase):
    def test_switch_on_off_on_without_restarting(self):
        upstream = Upstream('https://example.invalid/v1', lambda: None)
        auth = Authorization('synthetic', {'Authorization': 'synthetic'})
        settings = [{'https': 'http://127.0.0.1:7890'}, {}, {'https': 'http://127.0.0.1:7891'}]
        connections = [Mock(), Mock(), Mock()]
        with patch(MODULE+'getproxies', side_effect=settings), patch(MODULE+'proxy_bypass', return_value=False), patch(MODULE+'http.client.HTTPSConnection', side_effect=connections) as factory:
            for _ in range(3):
                with upstream.request('/responses', b'{}', {}, auth):
                    pass
        self.assertEqual([c.args[:2] for c in factory.call_args_list], [('127.0.0.1',7890),('example.invalid',None),('127.0.0.1',7891)])
        connections[0].set_tunnel.assert_called_once_with('example.invalid',443)
        connections[1].set_tunnel.assert_not_called()
        connections[2].set_tunnel.assert_called_once_with('example.invalid',443)
        for connection in connections:
            connection.request.assert_called_once()
            connection.close.assert_called_once()

    def test_bypass_uses_current_target_and_direct_connection(self):
        with patch(MODULE+'getproxies', return_value={'https':'http://127.0.0.1:7890'}), patch(MODULE+'proxy_bypass', return_value=True) as bypass:
            self.assertIsNone(resolve_proxy('https://example.invalid:443/v1'))
            bypass.assert_called_once_with('example.invalid:443')

    def test_loopback_fixture_does_not_consult_proxy(self):
        with patch(MODULE+'getproxies') as settings:
            self.assertIsNone(resolve_proxy('http://127.0.0.1:1234/v1'))
            settings.assert_not_called()

    def test_all_proxy_and_https_precedence(self):
        with patch(MODULE+'getproxies', return_value={'all':'http://localhost:7890'}), patch(MODULE+'proxy_bypass', return_value=False):
            self.assertEqual(resolve_proxy('https://example.invalid/v1').port,7890)
        with patch(MODULE+'getproxies', return_value={'all':'http://localhost:7890','https':'http://localhost:7891'}), patch(MODULE+'proxy_bypass', return_value=False):
            self.assertEqual(resolve_proxy('https://example.invalid/v1').port,7891)

    def test_invalid_proxy_is_sanitized_without_fallback(self):
        for value in ['http://user:synthetic@localhost:7890','http://localhost:bad','http://localhost:0','http://localhost:70000','http://localhost/path','socks5://localhost:7890','http://remote.invalid:7890']:
            with self.subTest(value=value), patch(MODULE+'getproxies', return_value={'https':value}), patch(MODULE+'proxy_bypass', return_value=False):
                with self.assertRaises(GatewayError) as caught:
                    resolve_proxy('https://example.invalid/v1')
                self.assertNotIn(value,str(caught.exception))

    def test_failed_proxy_connection_does_not_replay_request(self):
        connection=Mock();connection.connect.side_effect=ConnectionRefusedError()
        upstream=Upstream('https://example.invalid/v1',lambda:None)
        with patch(MODULE+'getproxies',return_value={'https':'http://localhost:7890'}), patch(MODULE+'proxy_bypass',return_value=False), patch(MODULE+'http.client.HTTPSConnection',return_value=connection) as factory:
            with self.assertRaises(GatewayError):
                with upstream.request('/responses',b'{}',{},Authorization('synthetic')):
                    pass
            self.assertEqual(factory.call_count,3)
            connection.request.assert_not_called()
            self.assertEqual(connection.close.call_count,3)

    def test_network_setting_change_during_connect_is_recovered_before_post(self):
        failed,ready=Mock(),Mock();failed.connect.side_effect=ConnectionRefusedError()
        upstream=Upstream('https://example.invalid/v1',lambda:None)
        with patch(MODULE+'getproxies',side_effect=[{'https':'http://localhost:7890'},{}]), patch(MODULE+'proxy_bypass',return_value=False), patch(MODULE+'time.sleep'), patch(MODULE+'http.client.HTTPSConnection',side_effect=[failed,ready]) as factory:
            with upstream.request('/responses',b'{}',{},Authorization('synthetic')):pass
            self.assertEqual(factory.call_args_list[1].args[:2],('example.invalid',None))
            failed.request.assert_not_called();failed.close.assert_called_once()
            ready.request.assert_called_once();ready.close.assert_called_once()

    def test_certificate_failure_is_not_retried(self):
        conn=Mock();conn.connect.side_effect=ssl.SSLCertVerificationError()
        with patch(MODULE+'getproxies',return_value={}), patch(MODULE+'http.client.HTTPSConnection',return_value=conn) as factory:
            with self.assertRaises(GatewayError) as caught:
                with Upstream('https://example.invalid/v1',lambda:None).request('/responses',b'{}',{},Authorization('synthetic')):pass
            self.assertEqual(caught.exception.code,'upstream_certificate_invalid')
            factory.assert_called_once();conn.request.assert_not_called()

    def test_failure_after_post_is_not_replayed(self):
        for stage in ['request','getresponse']:
            conn=Mock();getattr(conn,stage).side_effect=ConnectionResetError()
            with self.subTest(stage=stage), patch(MODULE+'getproxies',return_value={}), patch(MODULE+'http.client.HTTPSConnection',return_value=conn) as factory:
                with self.assertRaises(GatewayError) as caught:
                    with Upstream('https://example.invalid/v1',lambda:None).request('/responses',b'{}',{},Authorization('synthetic')):pass
                self.assertIn(caught.exception.code,('upstream_send_reset','upstream_response_reset'))
                factory.assert_called_once();conn.request.assert_called_once();conn.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
