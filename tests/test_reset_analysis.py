import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from codex_workbench.analyzer_worker import ResetAnalysisWorker
from codex_workbench.account_snapshot import AccountSnapshot
from codex_workbench.reset_analysis import PublicSourceFetcher, ResetAnalyzer, SourceEvidence, _parse_model_text, _reset_time, _reset_window, _history_summary, SafeRedirects


class ResetAnalysisTest(unittest.TestCase):
    def test_next_week_uses_the_public_post_calendar_not_model_arithmetic(self):
        from codex_workbench.reset_analysis import normalize_relative_reset_window
        now=datetime(2026,9,27,tzinfo=timezone.utc)
        evidence=SourceEvidence('post','https://x.com/thsottiaux/status/123',now.isoformat(),
                                text='More resets coming next week\n9:41 PM · Sep 26, 2026')
        original={'signal':'present','source_ids':['post'],'predicted_reset_window':{'basis':'公告下周重置'},'summary':'错误日期'}
        result=normalize_relative_reset_window(original.copy(),[evidence],now)
        self.assertEqual('2026-09-28T00:00:00+00:00',result['predicted_reset_window']['start'])
        self.assertEqual('2026-10-04T23:59:59+00:00',result['predicted_reset_window']['end'])
        self.assertIsNone(normalize_relative_reset_window(original.copy(),[],now)['predicted_reset_window'])

    def test_prediction_window_and_history_count_do_not_invent_data(self):
        now = datetime.now(timezone.utc)
        start, end = (now+timedelta(hours=1)).isoformat(), (now+timedelta(hours=3)).isoformat()
        self.assertEqual(start, _reset_window({'start':start,'end':end,'basis':'官方公告在此期间执行'}, now)['start'])
        for value in ({'start':end,'end':start,'basis':'wrong'}, {'start':start,'end':end}, {'start':start,'end':start,'basis':'point'}):
            self.assertIsNone(_reset_window(value, now))
        source = SourceEvidence('codex_resets', 'https://codex-resets.com/zh-CN', now.isoformat(), text='重置次数重置次数\n55\n平均重置间隔\n6.9天')
        summary = _history_summary([source], [{'event_type':'manual_quota_reset'}, {'event_type':'reset_card_grant'}])
        self.assertEqual(55, summary['reported_count'])
        self.assertEqual(1, summary['tracked_count'])
        self.assertIsNone(_history_summary([], [])['reported_count'])

    def test_absent_signal_requires_evidence_coverage_and_past_event_is_separate(self):
        client, fetcher = Mock(), Mock()
        source = SourceEvidence('thsottiaux', 'https://x.com/thsottiaux', 'now', text='public evidence')
        fetcher.fetch.return_value = source
        client.analyze.return_value = {'status':'likely_reset', 'signal':'none', 'source_ids':['thsottiaux'], 'last_manual_reset_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}
        analyzer=ResetAnalyzer(client, fetcher=fetcher, sources=(('thsottiaux','https://x.com/thsottiaux'),))
        self.assertEqual('none', analyzer.analyze_account({})['signal'])
        analyzer.sources += (('codex_official','https://openai.com/codex/'),)
        fetcher.fetch.side_effect = lambda name,url: source if name=='thsottiaux' else SourceEvidence(name,url,'now',error='source_unavailable')
        self.assertEqual('unknown', analyzer.analyze_account({})['signal'])

    def test_fetcher_rejects_non_allowlisted_urls(self):
        result = PublicSourceFetcher().fetch('bad', 'https://example.com/reset')
        self.assertEqual('source_not_allowed', result.error)

    def test_analysis_keeps_official_fields_and_uses_injected_model(self):
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, '2026-09-26T00:00:00+00:00', text='public reset history')

        class Client:
            def analyze(self, request):
                self.request = request
                return {'status': 'likely_reset', 'confidence': .83,
                        'source_ids': ['one'], 'last_manual_reset_at': (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
                        'predicted_reset_at': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                        'reset_card_likelihood': .42}

        account = {'remaining_percent': 7, 'resets_at': 200, 'reset_cards': 0}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('one', 'https://openai.com/codex/'),)).analyze_account(account)
        self.assertEqual('likely_reset', result['status'])
        self.assertEqual(.65, result['confidence'])
        self.assertEqual(7, account['remaining_percent'])
        self.assertEqual(200, account['resets_at'])
        self.assertFalse(result['stale'])

    def test_missing_model_is_explicit_unavailable(self):
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, 'now', error='source_unavailable')

        result = ResetAnalyzer(fetcher=Fetcher(), sources=(('one', 'https://openai.com/codex/'),)).analyze_account({'resets_at': 123})
        self.assertEqual('uncertain', result['status'])
        self.assertEqual('evidence_unavailable', result['error'])
        self.assertTrue(result['stale'])
        self.assertIsNone(result['predicted_reset_at'])
        self.assertIsNone(result['reset_card_likelihood'])

    def test_no_private_account_data_or_cycles_are_model_inputs(self):
        client = Mock()
        client.analyze.return_value = {'status': 'reset', 'confidence': 1, 'announced_reset_at': 'garbage'}
        fetcher = Mock()
        fetcher.fetch.return_value = SourceEvidence('codex_resets', 'https://codex-resets.com/zh-CN', 'now', text='third party history')
        result = ResetAnalyzer(client, fetcher=fetcher, sources=(('codex_resets','https://codex-resets.com/zh-CN'),)).analyze_account({'resets_at': 123, 'reset_cards': 9, 'email': 'private'})
        self.assertNotIn('account',client.analyze.call_args.args[0])
        self.assertNotIn('private',str(client.analyze.call_args))
        self.assertEqual('uncertain', result['status'])
        self.assertIsNone(result['predicted_reset_at'])
        self.assertIsNone(result['reset_card_likelihood'])

    def test_parser_and_date_validation(self):
        self.assertEqual({'status':'uncertain'}, _parse_model_text('<think>private reasoning</think>```json\n{"status":"uncertain"}\n```'))
        self.assertIsNone(_parse_model_text('<think>unfinished'))
        for value in [123, 'tomorrow', '2026-09-27T12:00:00', float('inf')]:
            self.assertIsNone(_reset_time(value))
        with self.assertRaises(ValueError):
            SafeRedirects().redirect_request(None, None, 302, '', {}, 'https://example.com/private')

    def test_global_cache_survives_restart_and_failure_preserves_success(self):
        with tempfile.TemporaryDirectory() as directory:
            analyzer = Mock()
            good = {'scope':'official_manual_reset','status':'uncertain','observed_at':datetime.now(timezone.utc).isoformat(),'history':[]}
            analyzer.analyze_account.return_value = good
            path=Path(directory)/'analysis.json'
            worker=ResetAnalysisWorker(analyzer,cache_path=path)
            worker.analyze_account({'id':'one'});worker.analyze_account({'id':'two'})
            analyzer.analyze_account.assert_called_once()
            restored=ResetAnalysisWorker(analyzer,cache_path=path)
            restored.analyze_account({});analyzer.analyze_account.assert_called_once()
            worker._next_attempt=0
            analyzer.analyze_account.side_effect=RuntimeError('failed')
            self.assertTrue(worker.analyze_account({})['stale'])
            self.assertEqual(good['observed_at'],worker.analyze_account({})['observed_at'])
            worker._next_attempt=0
            repeated=worker.analyze_account({})
            self.assertEqual(good['observed_at'],repeated['observed_at'])
            self.assertEqual(good['observed_at'],repeated['last_success_at'])

    def test_cache_read_stays_available_during_explicit_inference(self):
        import threading
        analyzer=Mock()
        worker=ResetAnalysisWorker(analyzer)
        self.assertIsNone(worker.cached())
        analyzer.analyze_account.assert_not_called()
        with worker._refresh_lock:
            finished=threading.Event()
            thread=threading.Thread(target=lambda: (worker.cached(),finished.set()),daemon=True)
            thread.start()
            self.assertTrue(finished.wait(1))
        thread.join(1)

    def test_snapshot_stores_analysis_separately_from_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            account = {'id': 'a', 'codex_home': directory, 'subject_id': 's', 'expected_email': 'a@example.invalid'}
            snapshot = AccountSnapshot(Path(directory))
            snapshot.save_usage(account, {'remaining_percent': 12, 'resets_at': 100, 'reset_cards': 0})
            snapshot.save_analysis(account, {'status': 'uncertain', 'confidence': 0.4, 'stale': False})
            cached = snapshot.cached(account)
            self.assertEqual(12, cached['remaining_percent'])
            self.assertEqual('uncertain', cached['reset_analysis']['status'])
            self.assertEqual(0, cached['reset_cards'])


if __name__ == '__main__':
    unittest.main()
