"""增量缓存的来源调用次数、增删改、账户边界、失败与容量验收。"""
import copy
import threading
import time
import unittest
from codex_workbench.view_cache import ViewCache,delta
from codex_workbench.catalog import validate
import test_readonly_service as readonly


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now=0;self.epoch='account-a';self.stamp='source-a';self.calls=0
        self.data={'skills':[{'id':'a','name':'A'},{'id':'b','name':'B'}],'status':{'observed_at':'first','state':'ok'}}
        self.cache=ViewCache(clock=lambda:self.now)
    def read(self):self.calls+=1;return copy.deepcopy(self.data)
    def sync(self,revision=None,key='agents',force=False):
        return self.cache.sync(key,revision,self.read,lambda:self.epoch,lambda:self.stamp,ttl=60,force=force)
    def test_cached_queries_skip_source_and_return_only_revision(self):
        first=self.sync();self.assertTrue(first['reset']);self.assertEqual(1,self.calls)
        for _ in range(3):
            same=self.sync(first['revision']);self.assertTrue(same['unchanged']);self.assertNotIn('data',same);self.assertNotIn('patch',same)
        self.assertEqual(1,self.calls)
    def test_incremental_add_modify_remove_and_order(self):
        first=self.sync();self.stamp='changed';self.data['skills']=[{'id':'c','name':'C'},{'id':'a','name':'A2'}]
        next=self.sync(first['revision']);self.assertFalse(next['reset']);patch=next['patch']['collections']['skills']
        self.assertEqual(['b'],patch['remove']);self.assertEqual(['c','a'],patch['order']);self.assertEqual(['c','a'],[x['id'] for x in patch['upsert']])
    def test_unchanged_items_not_transmitted(self):
        first=self.sync();self.stamp='changed';self.data['skills'][1]['name']='B2'
        change=self.sync(first['revision'])['patch']['collections']['skills'];self.assertEqual([{'id':'b','name':'B2'}],change['upsert'])
    def test_ttl_is_lazy_not_background_polling(self):
        first=self.sync();self.now=61;self.assertEqual(1,self.calls);self.assertTrue(self.sync(first['revision'])['unchanged']);self.assertEqual(2,self.calls)
    def test_observed_time_alone_does_not_change_revision(self):
        first=self.sync();self.data['status']['observed_at']='later';self.assertTrue(self.sync(first['revision'],force=True)['unchanged'])
    def test_force_reads_before_expiry(self):
        self.sync();self.sync(force=True);self.assertEqual(2,self.calls)
    def test_context_change_never_returns_previous_account_delta(self):
        first=self.sync();self.epoch='account-b';self.data['skills']=[]
        changed=self.sync(first['revision']);self.assertTrue(changed['reset']);self.assertNotIn('patch',changed);self.assertEqual('account-b',changed['context'])
    def test_context_change_during_read_is_rejected(self):
        def read():self.epoch='b';return self.data
        with self.assertRaisesRegex(ValueError,'账户环境'):
            self.cache.sync('x',None,read,lambda:self.epoch,lambda:self.stamp)
        self.assertEqual({},self.cache.entries)
    def test_failure_does_not_create_empty_success_revision(self):
        first=self.sync();self.data={'status':{'state':'error'}};self.now=100
        with self.assertRaises(ValueError):self.sync(first['revision'])
        self.data={'skills':[{'id':'b','name':'B'}]};fixed=self.sync(first['revision']);self.assertFalse(fixed['reset'])
    def test_source_changes_during_read_revalidated_next_time(self):
        def read():self.calls+=1;self.stamp=str(self.calls);return self.data
        self.cache.sync('a',None,read,lambda:self.epoch,lambda:self.stamp)
        self.cache.sync('a',None,read,lambda:self.epoch,lambda:self.stamp);self.assertEqual(2,self.calls)
    def test_views_and_scopes_have_independent_snapshots(self):
        for key in [('board','s1','page1'),('board','s1','page2'),('board','s2','page1'),('knowledge','a'),('knowledge','b')]:self.sync(key=key)
        self.assertEqual(5,self.calls)
    def test_evicted_revision_requires_full_reset(self):
        first=self.sync()
        for n in range(5):self.stamp=str(n);self.data['skills'][0]['name']=str(n);self.sync()
        self.assertTrue(self.sync(first['revision'])['reset'])
    def test_snapshot_not_mutable_by_caller(self):
        first=self.sync();first['data']['skills'].clear();self.assertEqual(2,len(self.sync()['data']['skills']))
    def test_memory_bounded_and_closed(self):
        self.cache.max_entries=2
        for n in range(5):self.sync(key=str(n))
        self.assertEqual(2,len(self.cache.entries));self.assertFalse(self.cache.inflight)
        self.cache.clear()
        with self.assertRaises(ValueError):self.sync()
    def test_same_query_concurrent_read_is_coalesced(self):
        entered=threading.Event();release=threading.Event();results=[]
        def read():self.calls+=1;entered.set();release.wait(2);return self.data
        def request():results.append(self.cache.sync('x',None,read,lambda:self.epoch,lambda:self.stamp))
        threads=[threading.Thread(target=request) for _ in range(3)]
        for t in threads:t.start()
        self.assertTrue(entered.wait(1));release.set()
        for t in threads:t.join(2)
        self.assertEqual(3,len(results));self.assertEqual(1,self.calls);self.assertFalse(self.cache.inflight)
    def test_empty_collections_and_non_id_arrays(self):
        patch=delta({'knowledge':[{'knowledge_key':'x'}],'tags':['a']},{'knowledge':[],'tags':['b']})
        self.assertEqual('knowledge_key',patch['collections']['knowledge']['key']);self.assertEqual(['x'],patch['collections']['knowledge']['remove']);self.assertEqual(['b'],patch['set']['tags'])


class SyncServiceTests(unittest.TestCase):
    setUp=readonly.ReadonlyWorkbenchTest.setUp
    tearDown=readonly.ReadonlyWorkbenchTest.tearDown
    def test_sync_view_contract_and_isolation(self):
        class Versions:
            def context(self):return 'test-account'
            def signature(self,view):return view
        self.service.source_versions=Versions()
        self.service.collect_snapshot('agents')
        first=self.service.call('workbench_sync',{'view':'agents'})
        same=self.service.call('workbench_sync',{'view':'agents','revision':first['revision']})
        self.assertTrue(same['unchanged']);self.assertEqual(['skills'],self.native.calls)
        for args in [{'view':'agents','session_id':'x'},{'view':'credential_details'},{'view':'config','public_key':'x'},{'view':'accounts','refresh':'yes'}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.service.call('workbench_sync',args)

    def test_entry_reuses_snapshot_and_supplies_bootstrap_revision(self):
        class Versions:
            def context(self):return 'account'
            def signature(self,view):return view
        self.service.source_versions=Versions()
        self.service.collect_snapshot('accounts')
        first=self.service.call('open_workbench',{})
        self.assertEqual('accounts',first['view']);self.assertIn('revision',first['_sync']);self.assertIn('accounts',first)
        self.service.call('open_workbench',{})
        self.assertEqual(1,self.native.calls.count('accounts'))
