"""本机默认路由的可用性与未接入提示；不访问真实账户。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codex_workbench.account_routing import default_routing_status, gateway_endpoint
from codex_workbench.codex_cli import resolve_gateway_cli


class DefaultRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        gateway = self.root / 'model-gateway'
        gateway.mkdir()
        (gateway / 'settings.json').write_text(json.dumps({'port': 18742}))
        (gateway / 'status.json').write_text(json.dumps({
            'ready': True, 'port': 18742, 'provider': 'workbench_gateway'}))
        self.config = self.root / 'config.toml'
        self.config.write_text('model_provider="workbench_gateway"\n[model_providers.workbench_gateway]\n'
                               'base_url="http://127.0.0.1:18742/v1"\nwire_api="responses"\nsupports_websockets=false\n')
        self.response = MagicMock(status=200)
        self.response.read.return_value = b'{"ready":true,"protocol":1}'
        self.client = MagicMock()
        self.client.getresponse.return_value = self.response
        self.patch = patch('codex_workbench.account_routing.http.client.HTTPConnection', return_value=self.client)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_ready_requires_live_health_and_native_provider(self):
        self.assertEqual(gateway_endpoint(self.root), 'http://127.0.0.1:18742/v1')
        self.assertTrue(default_routing_status(self.root, self.config)['routing_ready'])
        self.config.write_text('model_provider="openai"')
        self.assertEqual(default_routing_status(self.root, self.config)['routing_reason'], 'native_gateway_inactive')

    def test_stale_status_or_dead_gateway_never_reports_success(self):
        self.response.status = 503
        self.assertFalse(default_routing_status(self.root, self.config)['routing_ready'])
        with self.assertRaisesRegex(ValueError, '路由未就绪'):
            gateway_endpoint(self.root)
        self.response.status = 200
        (self.root/'model-gateway/status.json').write_text('{"ready":false}')
        self.assertFalse(default_routing_status(self.root, self.config)['routing_ready'])

    def test_official_cli_layout_migration_does_not_use_arbitrary_fallback(self):
        old = self.root / 'old-cli'
        new = self.root / 'new-cli'
        new.write_text('synthetic'); new.chmod(0o700)
        with patch('codex_workbench.codex_cli._APP_CLI_PATHS', (new, old)):
            self.assertEqual(resolve_gateway_cli(old), new)
            with self.assertRaisesRegex(ValueError, 'configured_codex_cli_unavailable'):
                resolve_gateway_cli(self.root / 'custom-missing-cli')


if __name__ == '__main__':
    unittest.main()
