"""模型刷新边界、分页统计与可用性分类。所有供应商调用使用合成数据。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_workbench.catalog import validate
from codex_workbench.model_discovery import parse_models, register_models, discover, SOURCES
from codex_workbench.model_catalog import configured_models
from codex_workbench.model_observations import ModelObservations
from codex_workbench.model_refresh import ModelRefresh
from codex_workbench.model_status import availability
from codex_workbench.service import _page_view


def document(models):
    return {'paths':{'/v1/chat/completions':{'post':{'requestBody':{'content':{'application/json':{
        'schema':{'$ref':'#/components/schemas/Request'}}}}}}},'components':{'schemas':{'Request':{
            'properties':{'model':{'enum':models}}}}}}


class DiscoveryTests(unittest.TestCase):
    def test_requests_only_and_stable(self):
        value=document(['MiniMax-M3','MiniMax-M4-Preview'])
        value['untrusted']={'model':'response-only'}
        rows=parse_models(value,'minimax','/v1/chat/completions','reasoning')
        self.assertEqual(['MiniMax-M3'],[m['model'] for m in rows])
        self.assertEqual('multimodal',rows[0]['model_type'])
        with self.assertRaises(ValueError):parse_models(document(['../../x']),'minimax','/v1/chat/completions','reasoning')

    def test_register_preserves_existing_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'models').mkdir()
            item={'id':'existing','name':'原名称','service_name':'MiniMax','updated_at':'2026-01-01T00:00:00Z','model':'MiniMax-M3','model_source':'explicit',
                  'model_type':'multimodal','provider_id':'minimax','provider_name':'MiniMax',
                  'base_url':'https://api.minimax.cn/v1/chat/completions','credential_id':'synthetic.reference'}
            path=root/'models/catalog.json';path.write_text(json.dumps({'version':1,'models':[item]}))
            before=path.read_bytes()
            candidates=parse_models(document(['MiniMax-M3','MiniMax-M4']),'minimax','/v1/chat/completions','reasoning')
            self.assertEqual(1,register_models(root,'minimax',candidates))
            self.assertEqual(0,register_models(root,'minimax',candidates))
            self.assertEqual(before,path.read_bytes())
            models=configured_models(path);new=next(m for m in models if m['id']!='existing')
            self.assertEqual('pending',new['validation_status'])
            self.assertEqual('synthetic.reference',new['credential_id'])
            with self.assertRaises(ValueError):register_models(root,'minimax',[{**candidates[1],'base_url':'https://evil.invalid'}])

    def test_failed_discovery_keeps_existing_and_duplicate_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ready=threading.Event();release=threading.Event()
            def blocked(provider):ready.set();release.wait(5);raise OSError('offline')
            task=ModelRefresh(root,root,discoverer=blocked,verifier=lambda *args:'available')
            task.start();self.assertTrue(ready.wait(2))
            self.assertTrue(ModelRefresh(root,root).start()['running'])
            release.set();task.thread.join(5)
            state=task.status();self.assertFalse(state['running'])
            self.assertEqual({'failed'}, {p['state'] for p in state['providers'].values()})
            self.assertFalse((root/'models/catalog.json').exists())


class ProjectionTests(unittest.TestCase):
    def test_filter_before_pagination_and_clamp(self):
        models=[{'id':str(i),'name':str(i),'provider_id':'minimax' if i%2 else 'bigmodel','model_type':'reasoning'} for i in range(45)]
        first=_page_view({'models':models},provider='minimax',page=1,page_size=10)
        last=_page_view({'models':models},provider='minimax',page=99,page_size=10)
        self.assertEqual({'page':3,'page_size':10,'total':22,'pages':3},last['pagination'])
        self.assertEqual(10,len(first['models']));self.assertEqual(2,len(last['models']))
        self.assertFalse({m['id'] for m in first['models']} & {m['id'] for m in last['models']})
        self.assertEqual(45,sum(p['count'] for p in last['facets']['providers']))
        for value in [0,-1,True]:
            with self.assertRaises(ValueError):validate('workbench_sync',{'view':'models','page':value})

    def test_payment_auth_transient_and_configuration_binding(self):
        for code,expected in [('payment_required','payment_required'),('insufficient_balance','insufficient_balance'),
                              ('credential_rejected','access_denied'),('not_found','unavailable'),('timeout','temporary_error')]:
            self.assertEqual(expected,availability('failed',code))
        with tempfile.TemporaryDirectory() as directory:
            store=ModelObservations(directory);model={'id':'m','credential_id':'synthetic.key'}
            store.record(model,success=False,source='probe',error_code='insufficient_balance')
            self.assertEqual('insufficient_balance',store.project([dict(model)])[0]['availability_status'])
            store.record(model,success=True,source='probe')
            self.assertEqual('available',store.project([dict(model)])[0]['availability_status'])
            changed={**model,'credential_id':'other.key','validation_status':'pending'}
            self.assertEqual('unverified',store.project([changed])[0]['availability_status'])
