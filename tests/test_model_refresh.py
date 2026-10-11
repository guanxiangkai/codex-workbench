"""模型刷新边界、分页统计与可用性分类。所有供应商调用使用合成数据。"""
import json
from pathlib import Path
import tempfile
import threading
import sys
import sqlite3
import time
import unittest
from unittest.mock import patch

from codex_workbench.catalog import validate, TOOLS
from codex_workbench.model_discovery import parse_models, register_models, discover, SOURCES
from codex_workbench.model_catalog import configured_models, registered_models
from codex_workbench.model_observations import ModelObservations
from codex_workbench.model_refresh import ModelRefresh, ProbeProcesses, verify_one
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
            speech={**candidates[1], 'model':'speech-3.0-hd', 'model_type':'text_to_speech',
                    'base_url':'https://api.minimax.cn/v1/t2a_v2'}
            self.assertEqual(1,register_models(root,'minimax',[speech]))
            speech_model=next(m for m in configured_models(path) if m['model']=='speech-3.0-hd')
            self.assertIsNone(speech_model['credential_id'])
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

    def test_legacy_models_are_deduplicated_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with sqlite3.connect(root/'workbench.sqlite3') as db:
                db.execute('CREATE TABLE provider_models(id TEXT,name TEXT,model_type TEXT,base_url TEXT,model TEXT,credential_ref TEXT)')
                db.execute('INSERT INTO provider_models VALUES(?,?,?,?,?,?)',
                           ('legacy','M3','multimodal','https://api.minimax.cn/v1/chat/completions','MiniMax-M3','vault:synthetic.reference'))
            candidate=parse_models(document(['MiniMax-M3']),'minimax','/v1/chat/completions','reasoning')
            seen=[]
            task=ModelRefresh(root,root,discoverer=lambda provider:candidate if provider=='minimax' else [],
                              verifier=lambda model,*args:seen.append(model['id']) or 'available')
            task.start();task.thread.join(3)
            self.assertFalse(task.thread.is_alive())
            self.assertEqual(['legacy'],seen);self.assertEqual(0,task.status()['added'])
            self.assertEqual('synthetic.reference',registered_models(root,root)[0]['credential_id'])
            self.assertEqual('minimax',registered_models(root,root)[0]['provider_id'])

    def test_close_waits_for_discovery_and_does_not_register_after_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ready=threading.Event();release=threading.Event();closed=threading.Event()
            candidate=parse_models(document(['MiniMax-M3']),'minimax','/v1/chat/completions','reasoning')
            def blocked(provider):ready.set();release.wait(3);return candidate
            notices=[]
            task=ModelRefresh(root,root,lambda:notices.append('update'),discoverer=blocked)
            task.start();self.assertTrue(ready.wait(2));before=len(notices)
            def close():task.close();closed.set()
            closer=threading.Thread(target=close);closer.start()
            self.assertTrue(task.stopping.wait(1));self.assertFalse(closed.is_set())
            release.set();closer.join(3)
            self.assertTrue(closed.is_set());self.assertFalse(task.thread.is_alive())
            self.assertEqual(before,len(notices));self.assertFalse(list((root/'models').glob('*.json')))
            self.assertFalse(task.status()['running'])

    def test_discovery_cancels_between_documents(self):
        calls=[]
        def fetch(url):calls.append(url);return document(['MiniMax-M3'])
        with self.assertRaises(InterruptedError):discover('minimax',fetch,cancelled=lambda:bool(calls))
        self.assertEqual(1,len(calls))


class ProjectionTests(unittest.TestCase):
    def test_skipped_validation_preserves_prior_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            model={'id':'m','provider_id':'minimax','credential_id':'synthetic.reference'}
            store=ModelObservations(root)
            store.record(model,success=True,source='probe')
            result={'model_id':'m','success':False,'status':'skipped','error_code':'unregistered_https_endpoint'}
            with patch.object(ProbeProcesses,'run',return_value=json.dumps(result).encode()):
                self.assertEqual('unsupported_validation',verify_one(model,root,root))
            self.assertEqual('available',store.project([dict(model)])[0]['availability_status'])

    def test_refresh_only_exposed_to_app_and_paused_is_preserved(self):
        tool=next(tool for tool in TOOLS if tool['name']=='model_refresh')
        self.assertEqual(['app'],tool['_meta']['ui']['visibility'])
        self.assertEqual('paused',availability('paused'))

    def test_close_terminates_probe_group_and_rejects_new_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ready=root/'ready';late=root/'late'
            child="import pathlib,sys,time;time.sleep(1.5);pathlib.Path(sys.argv[1]).write_text('unexpected')"
            parent="import pathlib,subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);pathlib.Path(sys.argv[3]).touch();time.sleep(30)"
            processes=ProbeProcesses();outcomes=[]
            def run():
                try:processes.run([sys.executable,'-c',parent,child,str(late),str(ready)])
                except InterruptedError:outcomes.append('cancelled')
            thread=threading.Thread(target=run);thread.start()
            deadline=time.monotonic()+3
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(ready.exists())
            processes.close();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(['cancelled'],outcomes)
            with self.assertRaises(InterruptedError):processes.run([sys.executable,'-c','pass'])
            time.sleep(1.6);self.assertFalse(late.exists())

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
