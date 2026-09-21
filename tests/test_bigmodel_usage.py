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
