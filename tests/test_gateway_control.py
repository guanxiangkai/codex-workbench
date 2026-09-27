"""仅恢复工具自己的 provider 选择，不覆盖用户的其他配置。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class GatewayReactivationTests(unittest.TestCase):
    def test_prepare_defaults_to_dynamic_and_explicitly_migrates_legacy_policy(self):
        cases = ((None, None, 'registered_accounts'),
                 ('fixed', None, 'fixed'),
                 ('fixed', 'registered_accounts', 'registered_accounts'),
                 (None, 'fixed', 'fixed'))
        for existing, override, expected in cases:
            with self.subTest(existing=existing, override=override), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                data = home / 'Library/Application Support/CodexWorkbench'
                root = data / 'model-gateway'
                root.mkdir(parents=True, mode=0o700)
                (home / 'Library/LaunchAgents').mkdir(parents=True)
                if existing:
                    (root / 'settings.json').write_text(json.dumps({'account_policy': existing}))
                with sqlite3.connect(data / 'workbench.sqlite3') as db:
                    db.execute('CREATE TABLE execution_accounts(id TEXT,codex_home TEXT,subject_id TEXT)')
                    db.execute('INSERT INTO execution_accounts VALUES(?,?,?)',
                               ('current', str(home / '.codex'), 'synthetic-current'))
                source = Path(__file__).resolve().parents[1] / 'gateway_control.py'
                spec = importlib.util.spec_from_file_location('gateway_policy_under_test', source)
                module = importlib.util.module_from_spec(spec)
                environment = {} if override is None else {'WORKBENCH_GATEWAY_ACCOUNT_POLICY': override}
                with patch.dict(os.environ, environment, clear=True), patch.object(Path, 'home', return_value=home):
                    spec.loader.exec_module(module)
                    with patch.object(module, 'resolve_gateway_cli', return_value=Path('/synthetic/codex')), contextlib.redirect_stdout(io.StringIO()):
                        module.prepare()
                settings = json.loads((root / 'settings.json').read_text())
                self.assertEqual(settings['account_policy'], expected)
                self.assertEqual(settings['authorized_account_ids'], ['current'])
                self.assertEqual(settings['accounts'][0]['subject_id'], 'synthetic-current')

    def test_reactivation_preserves_other_settings_and_refuses_changed_block(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = Path(__file__).resolve().parents[1] / 'gateway_control.py'
            spec = importlib.util.spec_from_file_location('gateway_control_under_test', source)
            module = importlib.util.module_from_spec(spec)
            with patch.object(Path, 'home', return_value=home):
                spec.loader.exec_module(module)
            module.ROOT.mkdir(parents=True)
            module.CONFIG.parent.mkdir(parents=True)
            for name in ('subscription-validation', 'official-cli-validation'):
                (module.ROOT / (name + '.json')).write_text('{"passed":true}')
            (module.ROOT / 'status.json').write_text('{"ready":true}')
            (module.ROOT / 'settings.json').write_text(json.dumps({'accounts': [{'id': 'current'}]}))
            disabled = module.TOP.replace('"workbench_gateway"', '"openai"')
            unrelated = '\nmodel = "synthetic-model"\n[desktop]\nexample = "preserve"\n'
            before = disabled + unrelated + module.BOTTOM
            module.CONFIG.write_text(before)
            response = MagicMock(status=200)
            response.read.return_value = b'{"ready":true}'
            client = MagicMock()
            client.getresponse.return_value = response
            with patch.object(module.http.client, 'HTTPConnection', return_value=client), contextlib.redirect_stdout(io.StringIO()):
                module.activate()
                self.assertEqual(module.CONFIG.read_text(), module.TOP + unrelated + module.BOTTOM)
                self.assertFalse(json.loads((module.ROOT / 'activation.json').read_text())['native_ui_verified'])
                formatted = before.replace('request_max_retries = 4', 'request_max_retries=4  # retained formatting')
                formatted = formatted.replace('# END CODEX WORKBENCH PROVIDER', '[browser_use]\nexample = true\n# END CODEX WORKBENCH PROVIDER')
                module.CONFIG.write_text(formatted)
                module.activate()
                self.assertEqual(module.CONFIG.read_text(), formatted.replace(disabled, module.TOP, 1))
                modified = before.replace('request_max_retries = 4', 'request_max_retries = 3')
                module.CONFIG.write_text(modified)
                with self.assertRaisesRegex(ValueError, '未覆盖'):
                    module.activate()
                self.assertEqual(module.CONFIG.read_text(), modified)


if __name__ == '__main__':
    unittest.main()
