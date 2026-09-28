import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from codex_workbench.analyzer_worker import ResetAnalysisWorker
from codex_workbench.account_snapshot import AccountSnapshot
from codex_workbench.reset_analysis import PublicSourceFetcher, ResetAnalyzer, SourceEvidence, _parse_model_text, _reset_time, _reset_window, _history_summary, _model_evidence_excerpt, _date_is_mentioned, SafeRedirects


class ResetAnalysisTest(unittest.TestCase):
    def test_explicit_feed_deadline_survives_model_omission_and_uses_beijing_summary(self):
        from codex_workbench.reset_analysis import _validated_predictions, _aggregate_predictions, _prediction_summary
        now = datetime(2026, 9, 28, 6, tzinfo=timezone.utc)
        source = SourceEvidence('codex_resets', 'https://codex-resets.com/zh-CN', now.isoformat(),
                                text='2天后可能重置 预计在 9月30日周三 06:59 UTC 前')
        candidates = _validated_predictions({'summary': '错误地换算为10月5日'}, [source], now)
        point, window, score, _, _ = _aggregate_predictions(candidates, [source], {'codex_resets'})
        self.assertEqual('2026-09-30T06:59:00+00:00', window['end'])
        summary = _prediction_summary(candidates, point, window, [source])
        self.assertIn('2026/09/30 14:59', summary)
        self.assertNotIn('10月5日', summary)
        self.assertNotIn('UTC', summary)
        self.assertGreater(score, 0)

    def test_direct_feed_deadlines_respect_beijing_and_new_year(self):
        from codex_workbench.reset_analysis import _validated_predictions
        now = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
        source = SourceEvidence('codex_resets', 'https://codex-resets.com/zh-CN', now.isoformat(),
                                text='预计在 1月1日周五 00:30 北京时间 前')
        result = _validated_predictions({}, [source], now)
        self.assertEqual('2026-12-31T16:30:00+00:00', result[0]['end'])
        self.assertEqual('Asia/Shanghai', result[0]['source_timezone'])
        for text in ('预计在 2026年1月1日周五 00:30 北京时间 前',
                     '预计在 1月1日周五 00:30 UTC+99 前',
                     '预计在 1月1日周五 00:30 前'):
            source = SourceEvidence('codex_resets', source.url, now.isoformat(), text=text)
            self.assertEqual([], _validated_predictions({}, [source], now))

    def test_direct_dated_post_uses_next_natural_week_without_model_dates(self):
        from codex_workbench.reset_analysis import _validated_predictions, _prediction_summary
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        source = SourceEvidence('thsottiaux_post_123', 'https://x.com/thsottiaux/status/123', now.isoformat(),
                                text='More resets coming next week 9:00 AM PT · Sep 27, 2026')
        result = _validated_predictions({}, [source], now)
        self.assertEqual('2026-09-28T07:00:00+00:00', result[0]['start'])
        self.assertEqual('2026-10-05T06:59:59+00:00', result[0]['end'])
        summary = _prediction_summary(result, None, result[0]['predicted_reset_window'], [source])
        self.assertIn('2026/09/28 15:00', summary)
        self.assertIn('2026/10/05 14:59', summary)

    def test_source_timezones_preserve_local_clock_and_us_dst(self):
        from codex_workbench.reset_analysis import _source_timezone, _matches_source_timezone
        for text, good, bad in [
            ('北京时间', '2026-09-30T06:59:00+08:00', '2026-09-30T06:59:00Z'),
            ('UTC', '2026-09-30T06:59:00Z', '2026-09-30T06:59:00+08:00'),
            ('Pacific Time', '2026-09-30T06:59:00-07:00', '2026-09-30T06:59:00-08:00'),
            ('Pacific Time', '2027-01-30T06:59:00-08:00', '2027-01-30T06:59:00-07:00'),
            ('Eastern Time', '2026-09-30T06:59:00-04:00', '2026-09-30T06:59:00-05:00'),
        ]:
            with self.subTest(source=text, time=good):
                zone, _ = _source_timezone(text)
                self.assertTrue(_matches_source_timezone(good, zone))
                self.assertFalse(_matches_source_timezone(bad, zone))
        self.assertIsNone(_source_timezone('X 美国用户发布的中文预测')[0])

    def test_next_week_source_calendar_handles_spring_dst(self):
        from codex_workbench.reset_analysis import _next_week_window
        now = datetime(2026, 3, 2, 1, tzinfo=timezone.utc)
        item = SourceEvidence('post', 'https://x.com/thsottiaux/status/123', now.isoformat(),
                              text='More resets coming next week\n9:41 PM Pacific Time · Mar 1, 2026')
        window = _next_week_window(item, now)
        self.assertEqual('2026-03-02T08:00:00+00:00', window['start'])
        self.assertEqual('2026-03-09T06:59:59+00:00', window['end'])

    def test_prediction_rejects_offset_conflicting_with_quoted_timezone(self):
        from codex_workbench.reset_analysis import _validated_predictions
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        quote = '预计北京时间 2026年9月30日 06:59 重置'
        source = SourceEvidence('codex_resets', 'https://codex-resets.com/zh-CN', now.isoformat(), text=quote)
        prediction = {'source_id': source.source, 'category': 'third_party_prediction', 'quote': quote,
                      'basis': '北京时间明确', 'predicted_reset_at': '2026-09-30T06:59:00Z'}
        self.assertEqual([], _validated_predictions({'predictions': [prediction]}, [source], now))
        prediction['predicted_reset_at'] = '2026-09-30T06:59:00+08:00'
        result = _validated_predictions({'predictions': [prediction]}, [source], now)
        self.assertEqual('2026-09-29T22:59:00+00:00', result[0]['start'])
        self.assertFalse(result[0]['timezone_estimated'])

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
        # Product pages do not constitute reset sources, even in an older source configuration.
        analyzer = ResetAnalyzer(client, fetcher=fetcher, sources=analyzer.sources)
        self.assertEqual('none', analyzer.analyze_account({})['signal'])

    def test_default_sources_include_replies_and_exclude_product_page(self):
        from codex_workbench.reset_analysis import DEFAULT_SOURCES
        self.assertIn(('thsottiaux', 'https://x.com/thsottiaux/with_replies'), DEFAULT_SOURCES)
        self.assertFalse(any('openai.com/codex' in url for _, url in DEFAULT_SOURCES))

    def test_unique_late_reply_is_retained_and_verified_before_shared_old_links(self):
        from codex_workbench.reset_analysis import _next_week_window, _validated_predictions
        now = datetime.now(timezone.utc)
        links = [f'https://x.com/thsottiaux/status/{value}' for value in (100, 99, 9)]
        radar = '\n'.join([
            links[0], now.isoformat(), '直接信号', 'All resets have propagated.',
            links[1], now.isoformat(), '无重置信号', 'unrelated text ' * 500,
            links[2], now.isoformat(), 'UTC', '直接信号',
            '英文原文@user More resets coming next week',
        ])
        observed = []
        class Fetcher:
            def fetch(self, name, url):
                observed.append((name, url))
                if name == 'codex_radar':
                    return SourceEvidence(name, url, now.isoformat(), text=radar, references=tuple(links))
                if name == 'codex_resets':
                    return SourceEvidence(name, url, now.isoformat(), text='Completed reset history', references=tuple(links[:2]))
                return SourceEvidence(name, url, now.isoformat(), error='source_unavailable')
        client = Mock()
        client.analyze.return_value = {
            'status': 'reset', 'signal': 'present', 'source_ids': ['codex_radar_post_9'],
            'last_manual_reset_at': (now - timedelta(hours=1)).isoformat(),
            'predictions': [{'source_id': 'codex_radar_post_9', 'category': 'reported_announcement',
                             'quote': 'More resets coming next week', 'basis': '下周的公开回复转录'}],
        }
        result = ResetAnalyzer(client, fetcher=Fetcher()).analyze_account({})
        self.assertIn(('thsottiaux_post_9', links[2]), observed)
        self.assertNotIn(('thsottiaux_post_99', links[1]), observed)
        request = client.analyze.call_args.args[0]
        excerpt = next(item for item in request['evidence'] if item['source'] == 'codex_radar_post_9')
        self.assertIn('More resets coming next week', excerpt['excerpt'])
        self.assertIn(now.isoformat(), excerpt['excerpt'])
        self.assertEqual('third_party', excerpt['authority'])
        self.assertEqual('present', result['signal'])
        self.assertEqual('likely_reset', result['status'])
        self.assertFalse(result['primary_verified'])
        candidate = result['prediction_candidates'][0]
        self.assertEqual(links[2], candidate['primary_post_url'])
        item = SourceEvidence('codex_radar_post_9', 'https://codexradar.com/', now.isoformat(),
                              text=excerpt['excerpt'], references=(links[2],))
        self.assertEqual(_next_week_window(item, now)['end'], candidate['end'])
        forged = dict(client.analyze.return_value['predictions'][0], category='official_announcement')
        checked = _validated_predictions({'predictions': [forged]}, [item], now)
        self.assertEqual(['reported_announcement'], [value['category'] for value in checked])
        self.assertIn('原文尚待核实', result['summary'])

    def test_reported_reply_calendar_uses_its_own_timestamp_and_timezone(self):
        from codex_workbench.reset_analysis import _next_week_window
        now = datetime(2026, 9, 28, 6, tzinfo=timezone.utc)
        item = SourceEvidence('codex_radar_post_9', 'https://codexradar.com/', now.isoformat(),
                              text='2026-09-27T05:41:35+08:00 北京时间\nMore resets coming next week',
                              references=('https://x.com/thsottiaux/status/9',))
        window = _next_week_window(item, now)
        self.assertEqual('2026-09-27T16:00:00+00:00', window['start'])
        self.assertEqual('2026-10-04T15:59:59+00:00', window['end'])
        wrong = SourceEvidence(item.source, item.url, item.observed_at,
                               text=item.text.replace('+08:00', 'Z'), references=item.references)
        self.assertIsNone(_next_week_window(wrong, now))

    def test_fetcher_rejects_non_allowlisted_urls(self):
        result = PublicSourceFetcher().fetch('bad', 'https://example.com/reset')
        self.assertEqual('source_not_allowed', result.error)

    def test_model_excerpt_keeps_late_third_party_prediction_context(self):
        text = '\n'.join(['历史重置记录'] * 700 + ['预计下周重置，截止日期按 UTC 估计。', '2026-10-01'])
        excerpt = _model_evidence_excerpt(text)
        self.assertIn('预计下周重置', excerpt)
        self.assertIn('2026-10-01', excerpt)
        self.assertLessEqual(len(excerpt.encode('utf-8')), 3800)

    def test_date_anchor_requires_stated_chinese_year_and_accepts_unqualified_dates(self):
        value = '2026-09-30T06:59:00+00:00'
        self.assertFalse(_date_is_mentioned(value, '预计在 2025年 9 月 30 日前重置'))
        self.assertTrue(_date_is_mentioned(value, '预计在 9 月 30 日前重置'))
        self.assertTrue(_date_is_mentioned(value, 'forecast before September 30'))

    def test_third_party_prediction_is_aggregated_without_official_confirmation(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        point = future.replace(microsecond=0).isoformat()
        quote = f'预计将在 {future:%Y-%m-%d} 重置额度'
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, '2026-09-26T00:00:00+00:00', text=quote)

        class Client:
            def analyze(self, request):
                self.request = request
                return {'status': 'uncertain', 'predictions': [
                    {'source_id': 'codex_resets', 'category': 'third_party_prediction', 'quote': quote,
                     'basis': '站点公开预测，时间按 UTC 表示', 'predicted_reset_at': point,
                     'predicted_reset_window': None}]}

        account = {'remaining_percent': 7, 'resets_at': 200, 'reset_cards': 0}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('codex_resets', 'https://codex-resets.com/zh-CN'),)).analyze_account(account)
        self.assertEqual('uncertain', result['status'])
        self.assertEqual(point, result['predicted_reset_at'])
        self.assertEqual('present', result['signal'])
        self.assertEqual('evidence_score', result['confidence_kind'])
        self.assertEqual(.325, result['confidence'])
        self.assertEqual(7, account['remaining_percent'])
        self.assertEqual(200, account['resets_at'])
        self.assertFalse(result['stale'])

    def test_multiple_candidates_show_combined_range_and_inconsistency_lowers_score(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first, last = now + timedelta(days=2), now + timedelta(days=16)
        range_end = last + timedelta(days=1)
        quotes = {'one': f'预测日期 {first:%Y-%m-%d}', 'two': f'预测区间 {last:%Y-%m-%d} 到 {range_end:%Y-%m-%d}'}
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, now.isoformat(), text=quotes[source])
        class Client:
            def analyze(self, request):
                return {'predictions': [
                    {'source_id': 'one', 'category': 'third_party_prediction', 'quote': quotes['one'], 'basis': '公开预测 UTC', 'predicted_reset_at': first.isoformat()},
                    {'source_id': 'two', 'category': 'third_party_prediction', 'quote': quotes['two'], 'basis': '公开预测 UTC',
                     'predicted_reset_window': {'start': last.isoformat(), 'end': range_end.isoformat(), 'basis': '公开预测 UTC'}}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(
            ('one', 'https://codex-resets.com/one'), ('two', 'https://codexradar.com/two'))).analyze_account({})
        self.assertIsNone(result['predicted_reset_at'])
        self.assertEqual(first.isoformat(), result['predicted_reset_window']['start'])
        self.assertEqual(range_end.isoformat(), result['predicted_reset_window']['end'])
        self.assertEqual(2, result['confidence_breakdown']['independent_sources'])
        self.assertLess(result['confidence'], .488)  # two agreeing point predictions would score .488

    def test_same_domain_reposts_do_not_add_independent_confidence(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
        quote = f'预测日期 {future:%Y-%m-%d}'
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, future.isoformat(), text=quote)
        class Client:
            def analyze(self, request):
                return {'predictions': [
                    {'source_id': 'copy_one', 'category': 'third_party_prediction', 'quote': quote, 'basis': '转载预测 UTC', 'predicted_reset_at': future.isoformat()},
                    {'source_id': 'copy_two', 'category': 'third_party_prediction', 'quote': quote, 'basis': '转载预测 UTC', 'predicted_reset_at': future.isoformat()}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(
            ('copy_one', 'https://codex-resets.com/one'), ('copy_two', 'https://codex-resets.com/two'))).analyze_account({})
        self.assertEqual(1, result['confidence_breakdown']['independent_sources'])
        self.assertEqual(1, result['confidence_breakdown']['time_measurement_count'])
        self.assertEqual(.325, result['confidence'])

    def test_chinese_quote_anchors_local_date_before_utc_normalization(self):
        local = timezone(timedelta(hours=8))
        future = (datetime.now(local) + timedelta(days=2)).replace(microsecond=0)
        date_text = str(future.year) + '年' + str(future.month) + '月' + str(future.day) + '日'
        quote = '预计在 ' + date_text + ' 重置额度'
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, future.isoformat(), text=quote)
        class Client:
            def analyze(self, request):
                return {'predictions': [{'source_id': 'codex_resets', 'category': 'third_party_prediction',
                                         'quote': quote, 'basis': '来源未给时区，按 UTC 对齐',
                                         'predicted_reset_at': future.isoformat()}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('codex_resets', 'https://codex-resets.com/zh-CN'),)).analyze_account({})
        self.assertEqual(future.astimezone(timezone.utc).isoformat(), result['predicted_reset_at'])

    def test_ongoing_window_is_kept_when_only_its_end_is_future(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = now - timedelta(hours=2), now + timedelta(days=1)
        start_text = str(start.year) + '年' + str(start.month) + '月' + str(start.day) + '日'
        end_text = str(end.year) + '年' + str(end.month) + '月' + str(end.day) + '日'
        quote = '预计窗口 ' + start_text + ' 至 ' + end_text
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, now.isoformat(), text=quote)
        class Client:
            def analyze(self, request):
                return {'predictions': [{'source_id': 'codex_resets', 'category': 'third_party_prediction',
                                         'quote': quote, 'basis': '公开窗口 UTC',
                                         'predicted_reset_window': {'start': start.isoformat(), 'end': end.isoformat(), 'basis': '公开窗口 UTC'}}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('codex_resets', 'https://codex-resets.com/zh-CN'),)).analyze_account({})
        self.assertEqual(start.isoformat(), result['predicted_reset_window']['start'])
        self.assertEqual(end.isoformat(), result['predicted_reset_window']['end'])

    def test_chinese_deadline_prediction_becomes_window(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        deadline = now + timedelta(days=2)
        quote = '2天后可能重置，预计在 ' + str(deadline.month) + '月' + str(deadline.day) + '日周三 06:59 UTC 前'
        deadline = deadline.replace(hour=6, minute=59, second=0)
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, now.isoformat(), text=quote)
        class Client:
            def analyze(self, request):
                return {'predictions': [{'source_id': 'codex_resets', 'category': 'third_party_prediction',
                                         'quote': quote, 'basis': 'DevDay 推测，截止时间按 UTC 估计',
                                         'predicted_reset_at': deadline.isoformat()}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('codex_resets', 'https://codex-resets.com/zh-CN'),)).analyze_account({})
        self.assertIsNone(result['predicted_reset_at'])
        self.assertEqual(deadline.isoformat(), result['predicted_reset_window']['end'])
        self.assertIn('截止时间', result['prediction_candidates'][0]['predicted_reset_window']['basis'])

    def test_past_or_uncited_prediction_is_not_a_future_candidate(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
        class Fetcher:
            def fetch(self, source, url):
                return SourceEvidence(source, url, past.isoformat(), text=f'重置已于 {past:%Y-%m-%d} 完成；另有页面导航日期 {(past + timedelta(days=10)):%Y-%m-%d}')
        class Client:
            def analyze(self, request):
                return {'predictions': [
                    {'source_id': 'codex_resets', 'category': 'historical_estimate', 'quote': f'重置已于 {past:%Y-%m-%d} 完成',
                     'basis': '历史事件', 'predicted_reset_at': (past + timedelta(days=10)).isoformat()}]}
        result = ResetAnalyzer(Client(), fetcher=Fetcher(), sources=(('codex_resets', 'https://codex-resets.com/zh-CN'),)).analyze_account({})
        self.assertIsNone(result['predicted_reset_at'])
        self.assertIsNone(result['confidence'])
        self.assertEqual([], result['prediction_candidates'])

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
            snapshot.save_analysis(account, {
                'status': 'uncertain', 'confidence': 0.4, 'stale': False,
                'confidence_breakdown': {'coverage': 1.0},
                'prediction_candidates': [{'source_id': 'one'}],
                'prediction_sources': [{'source_id': 'one', 'predictions': []}],
            })
            cached = snapshot.cached(account)
            self.assertEqual(12, cached['remaining_percent'])
            self.assertEqual('uncertain', cached['reset_analysis']['status'])
            self.assertEqual({'coverage': 1.0}, cached['reset_analysis']['confidence_breakdown'])
            self.assertEqual([{'source_id': 'one'}], cached['reset_analysis']['prediction_candidates'])
            self.assertEqual([{'source_id': 'one', 'predictions': []}], cached['reset_analysis']['prediction_sources'])
            self.assertEqual(0, cached['reset_cards'])


if __name__ == '__main__':
    unittest.main()
