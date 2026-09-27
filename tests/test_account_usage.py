"""官方用量转换、失败保留及页面刷新边界。"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from codex_workbench.account_usage import AccountUsage
from codex_workbench.minimax_usage_worker import ENDPOINT, NoRedirect, credential_key, normalize_usage, read_usage
from codex_workbench.other_accounts import other_accounts
from codex_workbench.service import Workbench
from readonly_fixture import Fixture


def payload():
    return {'base_resp': {'status_code': 0}, 'model_remains': [{
        'model_name': 'general', 'start_time': 1789610400000, 'end_time': 1789628400000,
        'weekly_start_time': 1789315200000, 'weekly_end_time': 1789920000000,
        'current_interval_total_count': 0, 'current_interval_usage_count': 0,
        'current_weekly_total_count': 0, 'current_weekly_usage_count': 0,
        'current_interval_remaining_percent': 100, 'current_weekly_remaining_percent': 93}]}


def snapshot(at='2026-09-17T04:00:00+00:00'):
    return normalize_usage(payload(), at)


class UsageTests(unittest.TestCase):
    def test_vault_payload_formats_and_invalid_credentials(self):
        for raw in (b'synthetic-test-key\n', b'{"api_key":"synthetic-test-key"}', b'"synthetic-test-key"'):
            self.assertEqual('synthetic-test-key', credential_key(raw))
        for raw in (b'', b'{}', b'null', b'[]', b'bad key', b'bad\r\nheader', b'x' * 65537):
            with self.assertRaises(ValueError):
                credential_key(raw)

    def test_percentages_and_millisecond_dates(self):
        result = snapshot()
        self.assertEqual(7, result['usage_windows'][1]['usage']['used'])
        self.assertEqual('2026-09-17T07:00:00+00:00', result['resets_at'])
        self.assertEqual('2026-09-20T16:00:00+00:00', result['usage_windows'][1]['resets_at'])
        for invalid in (True, -1, 101, float('nan'), '93'):
            data = payload()
            data['model_remains'][0]['current_weekly_remaining_percent'] = invalid
            with self.assertRaises(ValueError):
                normalize_usage(data, 'now')
        data = payload()
        data['model_remains'] *= 2
        with self.assertRaises(ValueError):
            normalize_usage(data, 'now')
        for base in (None, [], {'status_code': False}):
            data = payload()
            data['base_resp'] = base
            with self.assertRaises(ValueError):
                normalize_usage(data, 'now')

    def test_fixed_get_and_safe_errors(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload()).encode()
        with patch('codex_workbench.minimax_usage_worker.urllib.request.build_opener', return_value=opener):
            self.assertTrue(read_usage('synthetic-test-key')['ok'])
            request = opener.open.call_args.args[0]
            self.assertEqual(ENDPOINT, request.full_url)
            self.assertEqual('GET', request.method)
            self.assertEqual('Bearer synthetic-test-key', request.get_header('Authorization'))
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                {'base_resp': {'status_code': 1004, 'status_msg': 'synthetic-test-key'}}).encode()
            self.assertEqual({'ok': False, 'code': 'auth_rejected'}, read_usage('synthetic-test-key'))
            opener.open.side_effect = HTTPError(ENDPOINT, 401, 'synthetic-test-key', {}, None)
            self.assertEqual({'ok': False, 'code': 'auth_rejected'}, read_usage('synthetic-test-key'))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))

    def test_failed_refresh_preserves_last_success_and_isolates_binding(self):
        fetcher = Mock(side_effect=[{'ok': True, 'snapshot': snapshot()},
                                   {'ok': False, 'code': 'network_failed'},
                                   {'ok': False, 'code': 'auth_rejected'}])
        adapter = AccountUsage(fetcher)
        account = {'id': 'primary', 'provider_id': 'minimax', 'usage_credential_id': 'key.one'}
        first = adapter.refresh(account)
        second = adapter.refresh(account)
        self.assertEqual(first['usage_windows'], second['usage_windows'])
        self.assertEqual(first['updated_at'], second['updated_at'])
        self.assertEqual('failed', second['usage_refresh']['state'])
        third = adapter.refresh({**account, 'usage_credential_id': 'key.two'})
        self.assertNotIn('usage_windows', third)
        self.assertEqual('rejected', third['api_auth']['status'])
        self.assertEqual({}, adapter.refresh({**account, 'usage_credential_id': None}))
        self.assertEqual(3, fetcher.call_count)

    def test_runtime_cache_survives_restart_and_isolates_credential(self):
        account = {'id': 'primary', 'provider_id': 'minimax', 'usage_credential_id': 'key.one'}
        with tempfile.TemporaryDirectory() as directory:
            first_fetcher = Mock(return_value={'ok': True, 'snapshot': snapshot()})
            first = AccountUsage(first_fetcher, data_dir=directory)
            expected = first.refresh(account)
            cache = os.path.join(directory, 'account-usage-cache.json')
            self.assertEqual(0o600, os.stat(cache).st_mode & 0o777)
            with open(cache, encoding='utf-8') as handle:
                self.assertNotIn('key.one', handle.read())
            second_fetcher = Mock()
            restarted = AccountUsage(second_fetcher, data_dir=directory)
            self.assertEqual(expected['usage_windows'], restarted.cached(account)['usage_windows'])
            self.assertEqual({}, restarted.cached({**account, 'usage_credential_id': 'key.two'}))
            self.assertEqual({}, restarted.cached({**account, 'updated_at': '2030-01-01T00:00:00+00:00'}))
            second_fetcher.assert_not_called()

    def test_failed_only_and_corrupt_runtime_cache_are_safe(self):
        account = {'id': 'primary', 'provider_id': 'minimax', 'usage_credential_id': 'key.one'}
        with tempfile.TemporaryDirectory() as directory:
            cache = os.path.join(directory, 'account-usage-cache.json')
            AccountUsage(Mock(return_value={'ok': True, 'snapshot': snapshot()}), data_dir=directory).refresh(account)
            with open(cache, encoding='utf-8') as handle:
                corrupt = json.load(handle)
            corrupt['entries'][0]['snapshot'].update({'usage': {'used': 0}, 'usage_windows': 'not-a-list'})
            with open(cache, 'w', encoding='utf-8') as handle:
                json.dump(corrupt, handle)
            adapter = AccountUsage(Mock(return_value={'ok': False, 'code': 'network_failed'}), data_dir=directory)
            self.assertEqual({}, adapter.cached(account))
            failed = adapter.refresh(account)
            self.assertEqual('failed', failed['usage_refresh']['state'])
            self.assertEqual('failed', adapter.cached(account)['usage_refresh']['state'])

    def test_workbench_restart_collects_persisted_usage_without_refreshing(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        path = fixture.root / 'resources/accounts/catalog.json'
        path.parent.mkdir(parents=True)
        catalog_windows = [
            {'id': 'catalog_window_one', 'label': '目录窗口 1',
             'usage': {'used': 1, 'limit': 10, 'remaining': 9, 'unit': 'requests',
                       'observed_at': '2026-09-16T00:00:00+00:00', 'source': 'catalog'},
             'resets_at': '2026-09-16T05:00:00+00:00'},
            {'id': 'catalog_window_two', 'label': '目录窗口 2',
             'usage': {'used': 2, 'limit': 20, 'remaining': 18, 'unit': 'requests',
                       'observed_at': '2026-09-16T00:00:00+00:00', 'source': 'catalog'},
             'resets_at': '2026-09-23T00:00:00+00:00'},
        ]
        account = {'id': 'primary', 'label': 'MiniMax 主账户', 'usage_credential_id': 'key.one',
                   'usage_windows': catalog_windows}
        path.write_text(json.dumps({'version': 1, 'providers': [
            {'id': 'minimax', 'name': 'MiniMax', 'accounts': [account]}]}))
        fetcher = Mock(return_value={'ok': True, 'snapshot': snapshot()})
        fixture.board.account_usage = AccountUsage(fetcher, data_dir=fixture.root)
        fixture.board.refresh_provider_usage('minimax')

        versions = type('Versions', (), {'context': lambda self: 'fixture-context'})()
        restarted = Workbench(
            fixture.root, fixture.root / 'resources', native_reader=fixture.native,
            credential_catalog=fixture.credentials, credential_reader=fixture.board.credential_reader,
            source_versions=versions)
        self.addCleanup(restarted.close)
        restarted.collect_snapshot('other_accounts')
        view = restarted.call('workbench_sync', {'view': 'other_accounts'})
        self.assertEqual(snapshot()['usage_windows'], view['data']['accounts'][0]['usage_windows'])
        self.assertNotEqual(catalog_windows, view['data']['accounts'][0]['usage_windows'])
        fetcher.assert_called_once()

    def test_page_entry_refreshes_without_writing_catalog_or_querying_from_search(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        home = patch('codex_workbench.source_versions.current_account_home', return_value=str(fixture.root))
        home.start()
        self.addCleanup(home.stop)
        path = fixture.root / 'resources/accounts/catalog.json'
        path.parent.mkdir(parents=True)
        account = {'id': 'primary', 'label': 'MiniMax 主账户', 'usage_credential_id': 'key.one'}
        document = {'version': 1, 'providers': [{'id': 'minimax', 'name': 'MiniMax', 'accounts': [account]}]}
        path.write_text(json.dumps(document))
        before = path.read_bytes()
        fetcher = Mock(side_effect=[{'ok': True, 'snapshot': snapshot()},
                                   {'ok': True, 'snapshot': snapshot('2026-09-17T04:01:00+00:00')}])
        fixture.board.account_usage = AccountUsage(fetcher)
        fixture.board.call('workbench_sync', {'view': 'other_accounts'})
        fetcher.assert_not_called()
        fixture.board.refresh_provider_usage('minimax')
        first = fixture.board.call('workbench_sync', {'view': 'other_accounts'})
        fixture.board.refresh_provider_usage('minimax')
        fixture.board.collect_snapshot('other_accounts')
        second = fixture.board.call('workbench_sync', {'view': 'other_accounts'})
        self.assertEqual('2026-09-17T04:00:00+00:00', first['data']['accounts'][0]['updated_at'])
        self.assertEqual('2026-09-17T04:01:00+00:00', second['data']['accounts'][0]['updated_at'])
        fixture.board.collect_snapshot('config')
        fixture.board.call('workbench_state', {'view': 'config'})
        fixture.native.skill_sources = lambda: []
        fixture.board.call('global_search', {'query': 'MiniMax'})
        self.assertEqual(2, fetcher.call_count)
        self.assertEqual(before, path.read_bytes())
        account['usage_credential_id'] = '../bad'
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            other_accounts(path)


if __name__ == '__main__':
    unittest.main()
