"""模块拆分只产生读取视图，不复制数据、探测连接或展示内部验收记录。"""
import json
import subprocess
import unittest
from codex_workbench.catalog import MODULES,TOOLS,validate
from codex_workbench.knowledge_catalog import KnowledgeCatalog
from codex_workbench.connection_catalog import ConnectionCatalog
from codex_workbench.service_directory import services
from readonly_fixture import Fixture

class Runner:
    def __init__(self):self.calls=[]
    def __call__(self,args,**kwargs):
        self.calls.append((args,kwargs))
        op=args[1]
        if op=='scopes':values=[{'scope_key':'global','display_name':'通用','kind':'global','is_active':True},{'scope_key':'project:a','display_name':'项目 A','kind':'project','is_active':True}]
        elif op in ('catalog','search'):values=[{'scope_key':args[2],'knowledge_key':'rule.a','title':'规则','summary':'摘要','tags':[],'content':'private-body-not-in-list'}]
        elif op=='lookup':values=[{'scope_key':args[2],'knowledge_key':args[3],'revision':{'title':'规则','content':'正文','summary':'摘要','tags':[]},'source':{'native_id':'test','sensitivity':'internal'},'source_revision':{'revision_hash':'a'*64}}]
        else:raise AssertionError(op)
        return subprocess.CompletedProcess(args,0,'\n'.join(json.dumps(v) for v in values),'')

class ModuleCatalogTests(unittest.TestCase):
    def test_only_user_requested_modules_are_advertised(self):
        self.assertEqual({'agents','assets','knowledge','models','accounts','other_accounts','config'},{m['id'] for m in MODULES})
        self.assertFalse({'成果中心','运行诊断','验收记录'}&{m['name'] for m in MODULES})
        self.assertIn('技能助手',{m['name'] for m in MODULES})
        self.assertEqual(21,len(TOOLS));self.assertTrue(all(t['annotations']['readOnlyHint'] for t in TOOLS))
    def test_knowledge_list_scope_and_body_exclusion(self):
        runner=Runner();catalog=KnowledgeCatalog('/synthetic/knowledge',runner)
        result=catalog.listing('project:a','查询')
        self.assertEqual('project:a',result['selected_scope']);self.assertNotIn('content',result['knowledge'][0])
        self.assertEqual('查询',runner.calls[-1][1]['input']);self.assertNotIn('查询',runner.calls[-1][0])
        with self.assertRaises(ValueError):catalog.listing('project:unknown')
        self.assertEqual('正文',catalog.detail('project:a','rule.a')['knowledge']['content'])
    def test_knowledge_reader_never_performs_mutation(self):
        runner=Runner();catalog=KnowledgeCatalog('/synthetic/knowledge',runner)
        catalog.listing();catalog.detail('global','rule.a')
        self.assertTrue(all(args[1] in ('scopes','catalog','lookup') for args,_ in runner.calls))
        with self.assertRaises(ValueError):catalog._read(['approve','x'])
    def test_connections_drop_secrets_paths_commands_and_environment(self):
        def run(args,**kwargs):
            return subprocess.CompletedProcess(args,0,json.dumps([{'name':'tool','enabled':True,'auth_status':'unsupported','transport':{'type':'stdio','command':'private-path','env':{'TOKEN':'synthetic-secret'},'args':['secret-arg']}}]),'')
        class Native:
            def plugins(self):return [{'id':'plugin:x','name':'X','kind':'plugin','enabled':False,'availability':'unverified'}]
        result=ConnectionCatalog('/synthetic/codex',Native(),run).listing();encoded=json.dumps(result)
        for forbidden in ['synthetic-secret','private-path','secret-arg','TOKEN']:self.assertNotIn(forbidden,encoded)
        self.assertTrue(result['connections'][0]['enabled']);self.assertEqual('unverified',result['connections'][0]['availability'])
    def test_services_use_current_structure_not_labels(self):
        entries=[{'id':'a','label':'Redis','structure_status':'unindexed','has_fields':[]},
                 {'id':'b','label':'配置','structure_status':'indexed','has_fields':['host','password']},
                 {'id':'c','label':'Nacos','structure_status':'stale','has_fields':['host']}]
        result=services([],entries)
        self.assertEqual(['credential:b'],[x['id'] for x in result]);self.assertIsNone(result[0]['endpoint'])
    def test_models_and_vault_do_not_duplicate_same_service_reference(self):
        model={'id':'m','name':'模型','base_url':'https://example.invalid','credential_id':'b'}
        result=services([model],[{'id':'b','structure_status':'indexed','has_fields':['endpoint']}])
        self.assertEqual(1,len(result));self.assertEqual('model',result[0]['reference_kind'])
    def test_mcp_rejects_paths_mutations_and_diagnostic_views(self):
        for name,args in [('asset_detail',{'id':'x','path':'/etc/passwd'}),('knowledge_detail',{'scope':'global','key':'x','approve':True}),('workbench_state',{'view':'diagnostics'})]:
            with self.subTest(name=name),self.assertRaises(ValueError):validate(name,args)
    def test_project_modules_do_not_invent_associations(self):
        f=Fixture();self.addCleanup(f.close)
        f.native.snapshot=lambda:{'projects':[{'id':'p','name':'项目'}],'sections':[],'sessions':[{'id':'s','project_id':None}],'status':{'state':'ok'}}
        result=f.board.call('project_list',{});self.assertEqual(0,result['projects'][0]['session_count'])
        self.assertEqual([],f.board.call('project_detail',{'id':'p'})['sessions'])
        with self.assertRaises(ValueError):f.board.call('project_detail',{'id':'forged'})
        self.assertEqual([],list(f.root.iterdir()))


class ModuleSearchTests(unittest.TestCase):
    def test_search_never_searches_or_returns_secret_fields(self):
        f=Fixture();self.addCleanup(f.close)
        f.board.assets=type('Assets',(),{'list':lambda self:{'assets':[]}})()
        f.board.knowledge=type('Knowledge',(),{'listing':lambda self,*a:{'knowledge':[]}})()
        f.credentials.list=lambda:{'entries':[{'id':'entry','label':'公开名称','kind':'credential','password':'private-value'}],'folders':[],'status':{}}
        result=f.board.call('global_search',{'query':'private-value'})
        self.assertEqual([],result['results']);self.assertEqual([],f.secret_reads)
        result=f.board.call('global_search',{'query':'公开'})
        self.assertEqual('config',result['results'][0]['module']);self.assertNotIn('private-value',json.dumps(result))
    def test_source_errors_are_reported_without_private_exception_text(self):
        f=Fixture();self.addCleanup(f.close)
        class Broken:
            def list(self):raise ValueError('/private/source/details')
        f.board.assets=Broken();f.board.knowledge=type('Knowledge',(),{'listing':lambda self,*a:{'knowledge':[]}})()
        result=f.board.call('global_search',{'query':'skill'})
        self.assertIn('assets',result['source_errors']);self.assertNotIn('/private',json.dumps(result))

class ModuleConcurrencyTest(unittest.TestCase):
    def test_slow_account_read_does_not_block_model_catalog(self):
        import threading
        f=Fixture();self.addCleanup(f.close)
        started=threading.Event();release=threading.Event();finished=threading.Event()
        def accounts():started.set();release.wait(3);return []
        f.native.accounts=accounts
        account_thread=threading.Thread(target=lambda:f.board.call('workbench_state',{'view':'accounts'}))
        model_thread=threading.Thread(target=lambda:(f.board.call('model_list',{}),finished.set()))
        account_thread.start()
        try:
            self.assertTrue(started.wait(1));model_thread.start();self.assertTrue(finished.wait(.5))
        finally:
            release.set();account_thread.join(4)
            if model_thread.ident:model_thread.join(4)

if __name__=='__main__':unittest.main()
