"""调用结果与目录投影的一致性；全部使用合成配置，不访问模型服务。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from codex_workbench.model_observations import ModelObservations, configuration_fingerprint
from readonly_fixture import Fixture


class ModelObservationsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=Fixture();self.addCleanup(self.fixture.close)
        self.board=self.fixture.board;self.store=ModelObservations(self.fixture.root)
        self.catalog=self.fixture.root/'resources/models/catalog.json'
        self.catalog.parent.mkdir(parents=True)
        self.model={'id':'text','name':'合成文本模型','service_name':'合成服务','model_type':'reasoning',
                    'base_url':'https://example.invalid/v1/chat/completions','model':'synthetic',
                    'model_source':'explicit','credential_id':'synthetic-entry','api_profile':'openai_chat',
                    'updated_at':'2026-01-01T00:00:00Z'}
        self.save([self.model,{**self.model,'id':'unused','model':'unused'}])

    def save(self,models):self.catalog.write_text(json.dumps({'version':1,'models':models}))

    def test_read_does_not_create_state_and_success_is_per_model(self):
        self.assertEqual('pending',self.board.call('model_list',{})['models'][0]['validation_status'])
        self.assertFalse(self.store.path.exists())
        self.store.record(self.model,success=True,source='conversation',observed_at='2026-01-02T00:00:00Z')
        models=self.board.call('model_list',{})['models']
        self.assertEqual(['verified','pending'],[m['validation_status'] for m in models])
        detail=self.board.call('model_detail',{'id':'text'})['model']
        self.assertEqual('conversation',detail['validation_source'])
        self.assertTrue(self.board.collect_snapshot('models'))
        projected={item['id']:item for item in self.board.state('models')['models']}
        self.assertEqual('verified',projected['text']['validation_status'])
        reference=self.board.service_state()['services'][0]['models'][0]['id']
        self.assertEqual('verified',self.board.call('model_detail',{'id':reference})['model']['validation_status'])
        self.assertEqual([],self.fixture.secret_reads)
        self.assertNotIn('example.invalid',self.store.path.read_bytes().decode(errors='ignore'))

    def test_config_changes_invalidate_but_label_change_preserves_success(self):
        self.store.record(self.model,success=True,source='conversation')
        for field,value in [('base_url','https://another.invalid/v1/chat/completions'),('model','new'),
                            ('credential_id','different'),('api_profile',None),('model_type','multimodal')]:
            with self.subTest(field=field):
                self.save([{**self.model,field:value}])
                self.assertEqual('pending',self.board.call('model_list',{})['models'][0]['validation_status'])
        self.save([{**self.model,'name':'新的中文名称'}])
        self.assertEqual('verified',self.board.call('model_list',{})['models'][0]['validation_status'])

    def test_latest_failure_wins_over_late_history_and_success_recovers(self):
        self.store.record(self.model,success=False,source='conversation',observed_at='2026-01-03T00:00:00Z')
        self.store.record(self.model,success=True,source='conversation',observed_at='2026-01-02T00:00:00Z')
        model=self.board.call('model_list',{})['models'][0]
        self.assertEqual('failed',model['validation_status']);self.assertIsNotNone(model['last_verified_at'])
        self.store.record(self.model,success=True,source='conversation',observed_at='2026-01-04T00:00:00Z')
        self.assertEqual('verified',self.board.call('model_list',{})['models'][0]['validation_status'])

    def test_observation_updates_page_after_background_collection(self):
        class Versions:
            def context(self):return 'synthetic-context'
            def signature(self,view):return view
        self.board.source_versions=Versions()
        self.assertTrue(self.board.collect_snapshot('models'))
        cached=self.board.sync('models')
        before=self.board._model_catalog_revision()
        self.store.record(self.model,success=True,source='conversation')
        self.assertNotEqual(before,self.board._model_catalog_revision())
        self.assertEqual(cached['revision'],self.board.sync('models')['revision'])
        self.assertTrue(self.board.collect_snapshot('models'))
        updated=self.board.sync('models',revision=cached['revision'])
        self.assertNotEqual(cached['revision'],updated['revision'])
        projected={item['id']:item for item in self.board.sync('models')['data']['models']}
        self.assertEqual('verified',projected['text']['validation_status'])

    def test_pending_probe_preserves_history_without_claiming_a_terminal_result(self):
        from codex_workbench.registered_model_verifier import record_results
        self.store.record(self.model,success=True,source='probe',observed_at='2026-01-02T00:00:00Z')
        record_results(self.fixture.root,[self.model],[{'model_id':'text','success':False,
                       'status':'validating','error_code':'task_pending'}])
        model=self.board.call('model_detail',{'id':'text'})['model']
        self.assertEqual('validating',model['validation_status'])
        self.assertIsNotNone(model['last_verified_at'])
        self.assertIsNone(model['last_error_code'])
        self.store.record(self.model,success=True,source='probe')
        self.assertEqual('verified',self.board.call('model_detail',{'id':'text'})['model']['validation_status'])

    def test_invalid_evidence_is_rejected_before_write(self):
        for fields in ({'success':'true','source':'conversation'},{'success':True,'source':'account_usage'},
                       {'success':True,'source':'conversation','observed_at':'2026-01-01'},
                       {'success':True,'source':'conversation','observed_at':'2999-01-01T00:00:00Z'}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.store.record(self.model,**fields)
        self.assertFalse(self.store.path.exists())

    def test_conversation_command_requires_matching_call_configuration(self):
        command=[sys.executable,str(Path(__file__).resolve().parents[1]/'record_model_call.py'),
                 '--resources-dir',str(self.fixture.root/'resources'),'--data-dir',str(self.fixture.root),
                 '--model-id','text']
        result=subprocess.run(command,capture_output=True,text=True,check=True)
        fingerprint=result.stdout.strip()
        self.assertEqual(configuration_fingerprint(self.model),fingerprint)
        failed=subprocess.run(command+['--outcome','verified','--fingerprint','incorrect'],capture_output=True)
        self.assertNotEqual(0,failed.returncode);self.assertFalse(self.store.path.exists())
        subprocess.run(command+['--outcome','verified','--fingerprint',fingerprint],capture_output=True,check=True)
        self.assertEqual('verified',self.board.call('model_list',{})['models'][0]['validation_status'])


if __name__=='__main__':unittest.main()
