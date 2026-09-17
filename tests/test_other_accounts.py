"""其他账户受管目录的只读投影与配置中心关联契约。"""
import json
import unittest

from codex_workbench.other_accounts import other_accounts
from readonly_fixture import Fixture


class OtherAccountsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.path = self.fixture.root / 'resources/accounts/catalog.json'
        self.path.parent.mkdir(parents=True)
        self.account = {
            'id': 'zhipu-team', 'label': '智谱团队账户', 'vault_id': 'zhipu-key',
            'usage': {'used': 0, 'limit': None, 'remaining': 0, 'unit': 'tokens',
                      'observed_at': '2026-09-16T08:00:00+08:00', 'source': 'provider_console'},
            'is_used': True, 'status_source': 'catalog_registration',
            'observed_at': '2026-09-16T08:01:00+08:00', 'updated_at': '2026-09-16T08:02:00+08:00',
        }

    def save(self, providers):
        self.path.write_text(json.dumps({'version': 1, 'providers': providers}), encoding='utf-8')

    def test_missing_catalog_returns_empty_projection_without_touching_codex_account(self):
        self.path.unlink(missing_ok=True)
        state = self.fixture.board.call('workbench_state', {'view': 'other_accounts'})
        self.assertEqual([], state['providers'])
        self.assertEqual([], state['accounts'])
        self.assertEqual([], self.fixture.native.calls)
        self.assertEqual([], self.fixture.secret_reads)

    def test_custom_provider_flattens_accounts_and_preserves_zero_distinct_from_unknown(self):
        second = {**self.account, 'id': 'custom-free', 'label': '自定义账户', 'vault_id': None,
                  'usage': None, 'is_used': False, 'status_source': 'manual_audit',
                  'observed_at': '2026-09-16T08:03:00+08:00'}
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]},
                   {'id': 'custom-ai', 'name': '自定义平台', 'accounts': [second]},
                   {'id': 'deepseek', 'name': 'DeepSeek', 'accounts': []}])
        state = self.fixture.board.call('workbench_state', {'view': 'other_accounts'})
        self.assertEqual(['zhipu', 'custom-ai'], [item['id'] for item in state['providers']])
        self.assertEqual(['zhipu-team', 'custom-free'], [item['id'] for item in state['accounts']])
        self.assertEqual('自定义平台', state['accounts'][1]['provider_name'])
        self.assertEqual(0, state['accounts'][0]['usage']['used'])
        self.assertEqual(0, state['accounts'][0]['usage']['remaining'])
        self.assertIsNone(state['accounts'][1]['usage'])
        self.assertTrue(state['accounts'][0]['is_used'])
        self.assertEqual('catalog_registration', state['accounts'][0]['status_source'])
        self.assertEqual('other_account_catalog', state['accounts'][0]['source'])

    def test_absent_usage_status_is_unknown_and_vault_reference_matches_credential_format(self):
        account = {'id': 'unverified', 'label': '未登记用量账户', 'vault_id': '2.team.entry'}
        self.save([{'id': 'provider', 'name': '平台', 'accounts': [account]}])
        projected = other_accounts(self.path)['accounts'][0]
        self.assertEqual('2.team.entry', projected['vault_id'])
        self.assertIsNone(projected['usage'])
        self.assertIsNone(projected['is_used'])
        self.assertIsNone(projected['status_source'])
        self.assertIsNone(projected['observed_at'])

    def test_api_auth_usage_windows_and_field_notes_are_publicly_projected(self):
        account = {**self.account, 'is_used': None, 'status_source': None, 'observed_at': None,
                   'usage_windows': [
                       {'id': 'five_hour', 'label': '5 小时', 'usage': {'used': 20, 'limit': 100, 'remaining': 80, 'unit': '%', 'observed_at': '2026-09-16T08:00:00+08:00', 'source': 'provider_api'}, 'resets_at': '2026-09-16T13:00:00+08:00'},
                       {'id': 'weekly', 'label': '每周', 'usage': {'used': 0, 'limit': 100, 'remaining': 100, 'unit': '%', 'observed_at': '2026-09-16T08:00:00+08:00', 'source': 'provider_api'}, 'resets_at': None}],
                   'api_auth': {'status': 'accepted', 'source': 'provider_api', 'observed_at': '2026-09-16T08:00:00+08:00'},
                   'field_notes': {'expires_at': '接口未返回到期时间', 'last_used_at': '接口不提供最近使用时间'}}
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [account]}])
        projected = other_accounts(self.path)['accounts'][0]
        self.assertEqual('accepted', projected['api_auth']['status'])
        self.assertEqual(['five_hour', 'weekly'], [item['id'] for item in projected['usage_windows']])
        self.assertIsNone(projected['is_used'])
        self.assertEqual('接口未返回到期时间', projected['field_notes']['expires_at'])
        for invalid in ({'api_auth': {'status': 'accepted', 'source': 'provider_api'}}, {'api_auth': {'status': [], 'source': 'provider_api', 'observed_at': '2026-09-16T08:00:00+08:00'}}, {'usage_windows': [account['usage_windows'][0], account['usage_windows'][0]]}, {'usage_windows': [{**account['usage_windows'][0], 'usage': None}]}, {'field_notes': {'unexpected': 'x'}}):
            self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [{**account, **invalid}]}])
            with self.assertRaises(ValueError): other_accounts(self.path)

    def test_secret_fields_and_incomplete_or_naive_observations_are_rejected(self):
        cases = [
            {**self.account, 'api_key': 'must-not-appear'},
            {key: value for key, value in self.account.items() if key != 'status_source'},
            {**self.account, 'observed_at': '2026-09-16T08:00:00'},
            {**self.account, 'usage': {**self.account['usage'], 'used': None, 'limit': None, 'remaining': None}},
            {**self.account, 'usage': {**self.account['usage'], 'used': True}},
        ]
        for account in cases:
            with self.subTest(account=account):
                self.save([{'id': 'provider', 'name': '平台', 'accounts': [account]}])
                with self.assertRaises(ValueError):
                    other_accounts(self.path)

    def test_boolean_version_is_rejected(self):
        self.path.write_text(json.dumps({'version': True, 'providers': []}), encoding='utf-8')
        with self.assertRaises(ValueError):
            other_accounts(self.path)

    def test_config_retains_linked_model_credential_and_exposes_public_association(self):
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]}])
        self.fixture.credentials.list = lambda: {'entries': [{'id': 'zhipu-key', 'tags': [], 'label': '智谱密钥'}], 'folders': [], 'status': {'ready': True}}
        self.fixture.board._models = lambda: [{'id': 'model', 'credential_id': 'zhipu-key'}]
        state = self.fixture.board.call('workbench_state', {'view': 'config'})
        self.assertEqual([{'id': 'zhipu', 'name': '智谱'}], state['other_account_providers'])
        self.assertEqual(['zhipu-team'], state['entries'][0]['other_account_ids'])
        self.assertEqual(['zhipu'], state['entries'][0]['account_provider_ids'])

    def test_sync_uses_other_accounts_view_and_reloads_changed_catalog(self):
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]}])
        first = self.fixture.board.call('workbench_sync', {'view': 'other_accounts'})
        self.account['label'] = '智谱已更新账户'
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]}])
        changed = self.fixture.board.call('workbench_sync', {'view': 'other_accounts', 'revision': first['revision']})
        self.assertFalse(changed['unchanged'])
        self.assertEqual('智谱已更新账户', changed['patch']['collections']['accounts']['upsert'][0]['label'])

    def test_observation_time_change_is_sent_as_account_delta(self):
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]}])
        first = self.fixture.board.call('workbench_sync', {'view': 'other_accounts'})
        self.account['observed_at'] = '2026-09-16T08:09:00+08:00'
        self.account['usage']['observed_at'] = '2026-09-16T08:08:00+08:00'
        self.save([{'id': 'zhipu', 'name': '智谱', 'accounts': [self.account]}])
        changed = self.fixture.board.call('workbench_sync', {'view': 'other_accounts', 'revision': first['revision'], 'refresh': True})
        item = changed['patch']['collections']['accounts']['upsert'][0]
        self.assertEqual('2026-09-16T08:09:00+08:00', item['observed_at'])
        self.assertEqual('2026-09-16T08:08:00+08:00', item['usage']['observed_at'])


if __name__ == '__main__':
    unittest.main()
