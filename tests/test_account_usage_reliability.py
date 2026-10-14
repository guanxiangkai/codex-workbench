"""合成刷新边界：失败保留、响应校验和同绑定并发。"""
import threading
import unittest
from concurrent.futures import Future, TimeoutError
from unittest.mock import Mock, patch

from codex_workbench.account_usage import AccountUsage


ACCOUNT = {
    'id': 'synthetic-primary',
    'provider_id': 'minimax',
    'usage_credential_id': 'synthetic.key',
}


def snapshot(at='2026-10-15T00:00:00+00:00', used=100, remaining=900):
    return {
        'updated_at': at,
        'usage': {
            'used': used,
            'limit': 1000,
            'remaining': remaining,
            'unit': 'calls',
            'observed_at': at,
        },
        'usage_windows': [{
            'id': 'synthetic-window',
            'usage': {'used': used, 'limit': 1000, 'remaining': remaining,
                      'unit': 'calls', 'observed_at': at},
        }],
    }


class AccountUsageReliabilityTests(unittest.TestCase):
    def _successful_adapter(self):
        adapter = AccountUsage(Mock(return_value={'ok': True, 'snapshot': snapshot()}))
        self.assertEqual('ok', adapter.refresh(ACCOUNT)['usage_refresh']['state'])
        return adapter

    def _assert_failed_preserves(self, result, code):
        self.assertEqual(100, result['usage']['used'])
        self.assertEqual('2026-10-15T00:00:00+00:00', result['updated_at'])
        self.assertEqual('failed', result['usage_refresh']['state'])
        self.assertEqual(code, result['usage_refresh']['code'])

    def test_failure_keeps_success_data_then_later_success_replaces_it(self):
        fetcher = Mock(side_effect=[
            {'ok': True, 'snapshot': snapshot()},
            {'ok': False, 'code': 'network_failed'},
            {'ok': True, 'snapshot' : snapshot('2026-10-15T01:00:00+00:00', 250, 750)},
        ])
        adapter = AccountUsage(fetcher)

        first = adapter.refresh(ACCOUNT)
        failed = adapter.refresh(ACCOUNT)
        recovered = adapter.refresh(ACCOUNT)

        self._assert_failed_preserves(failed, 'network_failed')
        self.assertEqual(first['updated_at'], failed['updated_at'])
        self.assertEqual(first['usage']['observed_at'], failed['usage']['observed_at'])
        self.assertNotEqual(failed['updated_at'], failed['usage_refresh']['observed_at'])
        self.assertEqual(250, recovered['usage']['used'])
        self.assertEqual('2026-10-15T01:00:00+00:00', recovered['updated_at'])
        self.assertEqual(recovered['updated_at'], recovered['usage']['observed_at'])
        self.assertNotEqual(recovered['updated_at'], recovered['usage_refresh']['observed_at'])
        self.assertEqual('ok', recovered['usage_refresh']['state'])

    def test_auth_rejected_then_success_replaces_rejection(self):
        verified = snapshot('2026-10-15T01:00:00+00:00', 250, 750)
        verified['api_auth'] = {'status': 'verified'}
        adapter = AccountUsage(Mock(side_effect=[
            {'ok': True, 'snapshot': snapshot()},
            {'ok': False, 'code': 'auth_rejected'},
            {'ok': True, 'snapshot': verified},
        ]))

        adapter.refresh(ACCOUNT)
        rejected = adapter.refresh(ACCOUNT)
        recovered = adapter.refresh(ACCOUNT)

        self.assertEqual('rejected', rejected['api_auth']['status'])
        self.assertEqual('verified', recovered['api_auth']['status'])
        self.assertEqual(250, recovered['usage']['used'])

    def test_first_failure_is_unknown_and_does_not_invent_quota(self):
        result = AccountUsage(Mock(return_value={'ok': False, 'code': 'network_failed'})).refresh(ACCOUNT)

        self.assertNotIn('usage', result)
        self.assertNotIn('usage_windows', result)
        self.assertEqual('failed', result['usage_refresh']['state'])

    def test_malformed_responses_are_invalid_and_keep_previous_snapshot(self):
        bad_usage = snapshot()
        bad_usage['usage'] = 'bad'
        bad_windows = snapshot()
        bad_windows['usage_windows'] = 'bad'
        missing_updated_at = snapshot()
        del missing_updated_at['updated_at']
        malformed = (
            None,
            [],
            {'snapshot': snapshot()},
            {'ok': 'true', 'snapshot': snapshot()},
            {'ok': True},
            {'ok': True, 'snapshot': []},
            {'ok': True, 'snapshot': {}},
            {'ok': True, 'snapshot': bad_usage},
            {'ok': True, 'snapshot': bad_windows},
            {'ok': True, 'snapshot': missing_updated_at},
        )
        for response in malformed:
            with self.subTest(response=response):
                adapter = self._successful_adapter()
                adapter.fetcher = Mock(return_value=response)

                result = adapter.refresh(ACCOUNT)

                self._assert_failed_preserves(result, 'invalid_response')
                self.assertEqual(100, adapter.cached(ACCOUNT)['usage']['used'])

    def test_unexpected_owner_error_keeps_previous_snapshot_without_error_text(self):
        adapter = self._successful_adapter()
        adapter.fetcher = Mock(return_value={'ok': False, 'code': 'network_failed'})

        with patch.object(adapter, '_remember', side_effect=RuntimeError('synthetic-secret')):
            result = adapter.refresh(ACCOUNT)

        self._assert_failed_preserves(result, 'provider_unavailable')
        self.assertNotIn('synthetic-secret', str(result))

    def test_follower_timeout_returns_controlled_failure_and_owner_can_finish(self):
        started = threading.Event()
        release = threading.Event()
        owner_result = []
        owner_errors = []

        def fetcher(_credential):
            started.set()
            release.wait(5)
            return {'ok': True, 'snapshot': snapshot('2026-10-15T01:00:00+00:00', 200, 800)}

        adapter = self._successful_adapter()
        adapter.fetcher = fetcher
        def run_owner():
            try:
                owner_result.append(adapter.refresh(ACCOUNT))
            except BaseException as error:
                owner_errors.append(error)

        owner = threading.Thread(target=run_owner)
        owner.start()
        try:
            self.assertTrue(started.wait(5))
            with patch('codex_workbench.account_usage.Future.result', side_effect=TimeoutError):
                follower = adapter.refresh(ACCOUNT)

            self._assert_failed_preserves(follower, 'provider_unavailable')
            binding = adapter._binding(ACCOUNT)
            self.assertIn(binding, adapter.inflight)
            self.assertEqual('ok', adapter.cached(ACCOUNT)['usage_refresh']['state'])
        finally:
            release.set()
            owner.join(5)
        self.assertFalse(owner.is_alive())
        self.assertEqual([], owner_errors)
        self.assertEqual('ok', owner_result[0]['usage_refresh']['state'])
        self.assertEqual(200, adapter.cached(ACCOUNT)['usage']['used'])
        self.assertNotIn(binding, adapter.inflight)

    def test_concurrent_followers_share_one_owner_result(self):
        started = threading.Event()
        release = threading.Event()
        follower_waiting = threading.Event()
        calls = Mock()
        results = []
        errors = []

        def fetcher(_credential):
            calls()
            started.set()
            release.wait(5)
            return {'ok': True, 'snapshot': snapshot(used=200, remaining=800)}

        adapter = AccountUsage(fetcher)
        def run_refresh():
            try:
                results.append(adapter.refresh(ACCOUNT))
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=run_refresh)
        second = threading.Thread(target=run_refresh)
        first.start()
        try:
            self.assertTrue(started.wait(5))
            original_result = Future.result

            def observed_result(future, timeout=None):
                follower_waiting.set()
                return original_result(future, timeout)

            with patch('codex_workbench.account_usage.Future.result', autospec=True,
                       side_effect=observed_result):
                second.start()
                self.assertTrue(follower_waiting.wait(5))
                release.set()
        finally:
            release.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, calls.call_count)
        self.assertEqual([200, 200], sorted(result['usage']['used'] for result in results))


if __name__ == '__main__':
    unittest.main()
