"""只读工作台的公开边界、数据保真与无写入回归。"""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.catalog import TOOLS, validate
from codex_workbench.readonly_sources import NativeRead, ReadRpc, ReadonlyCredentials, connection, rows
from codex_workbench.service import Workbench


SESSION={'id':'native:session:s1','native_id':'s1','title':'会话','project_id':None,'section_id':None,'native_status':'unarchived'}
class FakeNative:
    def __init__(self):self.calls=[];self.missing=False
    def snapshot(self):
        self.calls.append('snapshot')
        return {'sessions':[] if self.missing else [dict(SESSION)],'projects':[],'sections':[],'status':{'state':'ok'}}
    def accounts(self):self.calls.append('accounts');return []
    def skills(self):self.calls.append('skills');return [{'id':'skill1','name':'Example','description':'说明'}]
class FakeCredentials:
    def __init__(self):self.calls=0
    def list(self):self.calls+=1;return {'entries':[],'folders':[],'status':{'ready':True}}


class ReadonlyWorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.native=FakeNative();self.credentials=FakeCredentials()
        self.patch=patch('codex_workbench.service.UiRelease');self.release=self.patch.start().return_value;self.release.revision='test'
        versions=type('Versions',(),{'context':lambda self:'fixture-context'})()
        self.service=Workbench(self.root,self.root/'resources',native_reader=self.native,credential_catalog=self.credentials,
                               credential_reader=lambda *args:{'ciphertext':'opaque','entry_id':args[1]},source_versions=versions)
    def tearDown(self):self.service.close();self.patch.stop();self.temp.cleanup()
    def test_constructor_and_read_views_do_not_create_business_files(self):
        for view in ['accounts','agents','models','config']:self.service.call('workbench_state',{'view':view})
        self.assertEqual([],list(self.root.iterdir()))
    def test_config_view_does_not_read_native_accounts_or_sessions(self):
        self.service.call('workbench_state',{'view':'config'})
        self.assertEqual([],self.native.calls);self.assertEqual(0,self.credentials.calls)
    def test_all_advertised_tools_read_only_and_old_writes_rejected(self):
        self.assertTrue(all(t['annotations']['readOnlyHint'] is (t['name']!='account_default') for t in TOOLS))
        for name in ['task_create','task_update','task_start','run_cancel','account_login','account_default','agent_update','model_validate','credential_update','section_create']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.service.call(name,{})
        self.assertEqual([],self.native.calls)
    def test_forged_context_or_missing_required_params_rejected(self):
        for name,args in [('workbench_state',{'view':'accounts','codex_home':'/tmp/other'}),('credential_details',{'id':'x','public_key':'k','request_id':'r','refreshToken':True})]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.service.call(name,args)

    def test_model_queries_preserve_database_and_hide_endpoint_secrets(self):
        db=self.root/'workbench.sqlite3'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE provider_models(id TEXT,name TEXT,base_url TEXT,credential_ref TEXT)')
            c.execute('INSERT INTO provider_models VALUES(?,?,?,?)',('m','M','https://example.invalid/v1?key=private','vault:entry'))
        before=hashlib.sha256(db.read_bytes()).hexdigest()
        value=self.service.call('model_list',{})['models'][0]
        self.assertEqual('',value['base_url']);self.assertEqual('entry',value['credential_id']);self.assertNotIn('credential_ref',value)
        self.assertEqual(before,hashlib.sha256(db.read_bytes()).hexdigest())
    def test_secret_read_is_explicit_and_returns_envelope_only(self):
        value=self.service.call('credential_details',{'id':'entry','public_key':'key','request_id':'request'})
        self.assertEqual({'ciphertext':'opaque','entry_id':'entry'},value)
        tool=next(t for t in TOOLS if t['name']=='credential_details')
        self.assertEqual(['app'],tool['_meta']['ui']['visibility'])
    def test_database_reader_refuses_writes(self):
        db=self.root/'test.sqlite'
        with sqlite3.connect(db) as c:c.execute('CREATE TABLE test(id TEXT)')
        with connection(db) as c,self.assertRaises(sqlite3.OperationalError):c.execute("INSERT INTO test VALUES('x')")
        self.assertEqual([],rows(db,'test',['id']))
    def test_credential_adapter_never_initializes_a_database(self):
        catalog=ReadonlyCredentials(self.root/'absent.sqlite')
        self.assertFalse(catalog.db_path.exists());self.assertEqual([],catalog._folders())
        view=catalog._entry_views({'entries':[{'entry_id':'item','revision':2}]})[0]
        self.assertEqual('unindexed',view['structure_status']);self.assertEqual('unknown',view['kind'])
        self.assertFalse(catalog.db_path.exists())
    def test_official_rpc_has_no_write_methods(self):
        self.assertEqual(ReadRpc.METHODS,ReadRpc.CURRENT_METHODS)
        self.assertFalse(any(x in ReadRpc.METHODS for x in ['account/login/start','thread/name/set','thread/start','turn/start','config/value/write']))


if __name__=='__main__':unittest.main()
