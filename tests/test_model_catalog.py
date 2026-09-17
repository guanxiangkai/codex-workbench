"""模型配置与服务投影共享密钥引用，不复制秘密或推断模型名称。"""
import json
import unittest
from codex_workbench.model_catalog import configured_models
from codex_workbench.service import _model_search_text
from codex_workbench.service_directory import services
from readonly_fixture import Fixture

class ConfiguredModelTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close);self.path=self.f.root/'resources/models/catalog.json';self.path.parent.mkdir(parents=True)
        self.model={'id':'image','name':'Image','service_name':'Image service','model_type':'image_generation','base_url':'https://example.invalid/v1/images/generations','model_source':'server_default','model':'','credential_id':'entry','updated_at':'2026-09-13T00:00:00Z'}
    def save(self,item):self.path.write_text(json.dumps({'version':1,'models':[item]}))
    def test_both_directories_share_reference(self):
        self.save(self.model);models=self.f.board.call('model_list',{})['models'];self.assertEqual(1,len(models));self.assertEqual('server_default',models[0]['model_source']);self.assertEqual('',models[0]['model'])
        result=services(models,[{'id':'entry','structure_status':'indexed','has_fields':['endpoint','key']}]);self.assertEqual(1,len(result));self.assertEqual('Image service',result[0]['name']);self.assertEqual('image',result[0]['models'][0]['id']);self.assertEqual('entry',result[0]['credential_id']);self.assertEqual([],self.f.secret_reads)
        detail=self.f.board.call('model_detail',{'id':'image'});self.assertEqual('image_generation',detail['model']['model_type'])
    def test_credentials_and_ambiguous_model_are_rejected(self):
        for changes in [{'key':'synthetic-secret'},{'base_url':'https://example.invalid?key=synthetic'},{'model_source':'server_default','model':'invented'},{'model_type':'invented'}]:
            self.save({**self.model,**changes})
            with self.assertRaises(ValueError):configured_models(self.path)
    def test_multiple_models_share_one_service(self):
        self.save(self.model);models=configured_models(self.path);models.append({**models[0],'id':'second','name':'Second'})
        result=services(models,[]);self.assertEqual(1,len(result));self.assertEqual(2,len(result[0]['models']))
    def test_catalog_change_invalidates_signature(self):
        self.save(self.model);before=self.f.board._model_catalog_revision();self.save({**self.model,'name':'Updated model name'});self.assertNotEqual(before,self.f.board._model_catalog_revision())

    def test_network_scope_is_explicit_and_preserved(self):
        for scope in ('internal','external'):
            self.save({**self.model,'network_scope':scope})
            self.assertEqual(scope,self.f.board.call('model_list',{})['models'][0]['network_scope'])
            self.assertEqual(scope,self.f.board.call('model_detail',{'id':'image'})['model']['network_scope'])
        self.save(self.model)
        self.assertNotIn('network_scope',configured_models(self.path)[0])
        self.save({**self.model,'network_scope':'guessed'})
        with self.assertRaises(ValueError):configured_models(self.path)

    def test_optional_provider_metadata_is_preserved_and_requires_a_pair(self):
        self.save({**self.model,'provider_id':'minimax','provider_name':'MiniMax'})
        model=self.f.board.call('model_list',{})['models'][0]
        self.assertEqual(('minimax','MiniMax'),(model['provider_id'],model['provider_name']))
        self.assertEqual('MiniMax',self.f.board.call('model_detail',{'id':'image'})['model']['provider_name'])
        for changes in ({'provider_id':'minimax'},{'provider_name':'MiniMax'},{'provider_id':'MiniMax','provider_name':'MiniMax'},{'provider_id':'minimax','provider_name':''}):
            self.save({**self.model,**changes})
            with self.assertRaises(ValueError):configured_models(self.path)

    def test_global_search_preserves_model_fields_and_minmax_alias(self):
        model={**self.model,'name':'MiniMax M3','model':'MiniMax-M3','service_name':'MiniMax service','model_type':'multimodal','provider_id':'minimax','provider_name':'MiniMax'}
        searchable=_model_search_text(model)
        for query in ('minmax','multimodal','MiniMax-M3','MiniMax service','minimax'):
            self.assertIn(query.casefold(),searchable)

    def test_missing_credential_is_explicit_without_a_fake_reference(self):
        self.save({key:value for key,value in self.model.items() if key!='credential_id'})
        model=self.f.board.call('model_list',{})['models'][0]
        self.assertIsNone(model.get('credential_id'))
        self.assertEqual('missing_credential',model['configuration_status'])
        self.save({**self.model,'credential_id':None})
        self.assertEqual('missing_credential',configured_models(self.path)[0]['configuration_status'])
        self.save({**self.model,'credential_id':''})
        with self.assertRaises(ValueError):configured_models(self.path)

    def test_protocol_reference_is_not_inferred_or_verified(self):
        self.save(self.model)
        self.assertIsNone(self.f.board.call('model_detail',{'id':'image'})['api_contract'])
        self.save({**self.model,'api_profile':'openai_images'})
        detail=self.f.board.call('model_detail',{'id':'image'})
        self.assertEqual('reference',detail['api_contract']['status'])
        self.assertEqual('pending',detail['model']['validation_status'])
        self.assertEqual('prompt',detail['api_contract']['inputs'][0][0])
        for value in ('invented',[],{},'openai_chat'):
            self.save({**self.model,'api_profile':value})
            with self.assertRaises(ValueError):configured_models(self.path)
