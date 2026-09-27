import unittest
from unittest.mock import Mock, patch, MagicMock
import json
from codex_workbench.bigmodel_usage_worker import normalize_usage, read_usage, ENDPOINT
from codex_workbench.account_usage import AccountUsage


def payload():
    return {'success':True,'code':200,'data':{'limits':[
        {'type':'CREDIT_LIMIT','percentage':25},
        {'type':'CREDIT_LIMIT','percentage':0,'nextResetTime':1790564085999}]}}


class BigmodelUsageTest(unittest.TestCase):
    def test_accepts_versioned_response_without_success_and_string_code(self):
        data = payload()
        data.pop('success')
        data['code'] = '200'
        value = normalize_usage(data, 'now')
        self.assertEqual(75, value['usage']['remaining'])

    def test_accepts_nested_data_envelope(self):
        data = payload()
        data['data'] = {'data': data['data']}
        self.assertEqual(75, normalize_usage(data, 'now')['usage']['remaining'])

    def test_official_percentage_and_missing_reset_are_preserved(self):
        value=normalize_usage(payload(),'2026-09-21T00:00:00+00:00')
        self.assertEqual(75,value['usage']['remaining'])
        self.assertIsNone(value['resets_at'])
        self.assertIsNotNone(value['usage_windows'][1]['resets_at'])
        self.assertEqual(ENDPOINT,value['api_auth']['source'])
        for invalid in (True,-1,101,float('nan'),'10'):
            data=payload();data['data']['limits'][0]['percentage']=invalid
            with self.assertRaises(ValueError):normalize_usage(data,'now')

    def test_provider_binding_and_read_only_cached_path(self):
        minimax=Mock();bigmodel=Mock(return_value={'ok':True,'snapshot':normalize_usage(payload(),'2026-09-21T00:00:00+00:00')})
        service=AccountUsage(minimax,bigmodel)
        account={'id':'primary','provider_id':'bigmodel','usage_credential_id':'synthetic.key'}
        self.assertEqual({},service.cached(account));bigmodel.assert_not_called()
        service.refresh(account)
        self.assertEqual(75,service.cached(account)['usage']['remaining'])
        self.assertEqual({},service.cached({**account,'provider_id':'minimax'}))
        minimax.assert_not_called();bigmodel.assert_called_once_with('synthetic.key')

    def test_fixed_official_get_and_no_error_body(self):
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload()).encode()
        with patch('codex_workbench.bigmodel_usage_worker.urllib.request.build_opener',return_value=opener):
            self.assertTrue(read_usage('synthetic-key')['ok'])
            request=opener.open.call_args.args[0]
            self.assertEqual(ENDPOINT,request.full_url)
            self.assertEqual('GET',request.method)
            self.assertEqual('synthetic-key',request.get_header('Authorization'))
            opener.open.return_value.__enter__.return_value.read.return_value=b'{"secret":"synthetic-key"}'
            self.assertEqual({'ok':False,'code':'invalid_response'},read_usage('synthetic-key'))


class CodingPlanCreditTest(unittest.TestCase):
    def payload(self):
        """脱敏的官方响应形状，积分字段与官方个人套餐概览一致。"""
        return {'success': True, 'code': 200, 'data': {'limits': [
            {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'usage': 2000,
             'currentValue': 0, 'remaining': 2000, 'percentage': 0},
            {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'usage': 10000,
             'currentValue': 0, 'remaining': 10000, 'percentage': 0,
             'nextResetTime': 1790564085999}]}}

    def test_credit_totals_windows_reset_and_order(self):
        data = self.payload()
        data['data']['limits'].reverse()
        value = normalize_usage(data, 'now')
        first, week = value['usage_windows']
        self.assertEqual(('coding-five-hour', 'Coding Plan · 5 小时'), (first['id'], first['label']))
        self.assertEqual((0, 2000, 2000, '积分'), tuple(first['usage'][k] for k in ('used', 'limit', 'remaining', 'unit')))
        self.assertEqual('coding-week', week['id'])
        self.assertEqual(10000, week['usage']['limit'])
        self.assertEqual('2026-09-28T02:54:45.999000+00:00', week['resets_at'])
        self.assertIsNone(first['resets_at'])
        data['data']['limits'][1].update(currentValue=123, remaining=1877, percentage=6)
        self.assertEqual(123, normalize_usage(data, 'now')['usage']['used'])

    def test_invalid_credit_values_and_duplicate_windows(self):
        for change in ({'currentValue': True}, {'usage': None}, {'remaining': -1},
                       {'remaining': 1998}, {'usage': float('inf')}):
            with self.subTest(change=change):
                data = self.payload(); data['data']['limits'][0].update(change)
                with self.assertRaises(ValueError): normalize_usage(data, 'now')
        data = self.payload(); data['data']['limits'][1] = dict(data['data']['limits'][0])
        with self.assertRaises(ValueError): normalize_usage(data, 'now')

    def test_official_one_credit_discrepancy_is_preserved(self):
        data = self.payload()
        data['data']['limits'][1].update(currentValue=2,remaining=9997,percentage=1)
        usage=normalize_usage(data,'now')['usage_windows'][1]['usage']
        self.assertEqual(9997,usage['remaining'])
        self.assertEqual(2,usage['used'])
        self.assertEqual(1,usage['rounding_difference'])

    def test_unknown_window_is_not_mislabeled(self):
        data = self.payload(); data['data']['limits'][0]['unit'] = 99
        windows = normalize_usage(data, 'now')['usage_windows']
        unknown = next(w for w in windows if w['id'] == 'quota-1')
        self.assertIn('未识别窗口', unknown['label'])
        self.assertEqual('积分', unknown['usage']['unit'])
