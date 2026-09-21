import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile

from codex_workbench.service import Workbench, _page_view
from codex_workbench.source_versions import SourceVersions


class DirectorySignalTest(unittest.TestCase):
    def test_directory_scan_is_reused_but_refresh_and_expiry_rescan(self):
        versions=SourceVersions('/tmp/synthetic.db','/tmp/synthetic')
        with patch('codex_workbench.source_versions.tree',return_value=['first']) as scan, patch('codex_workbench.source_versions.time.monotonic',return_value=10) as clock:
            self.assertEqual(['first'],versions._tree('/tmp/synthetic'))
            versions._tree('/tmp/synthetic')
            self.assertEqual(1,scan.call_count)
            versions.invalidate()
            versions._tree('/tmp/synthetic')
            self.assertEqual(2,scan.call_count)
            clock.return_value=13
            versions._tree('/tmp/synthetic')
            self.assertEqual(3,scan.call_count)


class PaginationShapeTest(unittest.TestCase):
    def test_realistic_model_shape_pages_without_duplicates(self):
        rows=[{'id':f'm{i}','name':f'MiniMax {i}','model_type':'chat',
               'provider_id':'minimax','provider_name':'MiniMax',
               'tags':['minmax'] if i % 2 == 0 else []} for i in range(25)]
        first=_page_view({'models':rows},page=1,page_size=20,query='minmax')
        second=_page_view({'models':rows},page=2,page_size=20,query='minmax')
        self.assertEqual(25,first['pagination']['total'])
        self.assertEqual(20,len(first['models']))
        self.assertEqual(5,len(second['models']))
        self.assertTrue(set(x['id'] for x in first['models']).isdisjoint(x['id'] for x in second['models']))
        self.assertEqual(2,first['pagination']['total_pages'])

    def test_accounts_do_not_leak_full_provider_lists(self):
        rows=[{'id':str(i),'provider_id':'glm','is_current':i==24} for i in range(25)]
        result=_page_view({'accounts':rows,'providers':[{'id':'glm','name':'智谱','accounts':rows}]})
        self.assertEqual(20,len(result['accounts']))
        self.assertEqual('24',result['accounts'][0]['id'])
        self.assertNotIn('accounts',result['providers'][0])

    def test_empty_shape_still_has_pagination(self):
        result=_page_view({'models':[]},page=1,page_size=20)
        self.assertEqual({'page':1,'page_size':20,'total':0,'total_pages':0},result['pagination'])

    def test_filter_aliases_and_array_tags(self):
        value={'entries':[{'id':'a','kind':'service','service_type':'chat','service_type_label':'对话','account_provider_ids':['glm'],'tags':['production']}]}
        self.assertEqual(1,len(_page_view(value,kind='chat')['entries']))
        self.assertEqual(1,len(_page_view(value,provider='glm',tag='production')['entries']))

    def test_folders_can_be_counted_from_real_tree_shape(self):
        rows=[{'id':'a','folder_id':'grandchild'},
              {'id':'b','folder_id':'root','folder_parent_id':None}]
        result=_page_view({'entries':rows,'folders':[{'id':'root','parent_id':None},{'id':'child','parent_id':'root'},{'id':'grandchild','parent_id':'child'}]},folder='root')
        self.assertEqual(2,result['pagination']['total'])
        self.assertEqual(2,result['facets']['folder_counts']['root'])
        self.assertEqual(1,result['facets']['folder_counts']['child'])


class SnapshotPaginationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.native=Mock()
        self.native.skills=Mock(return_value=[{'id':str(i),'display_name':str(i)} for i in range(25)])
        self.src=Mock(context=Mock(return_value='ctx'))
        self.patch=patch('codex_workbench.service.UiRelease')
        self.patch.start().return_value.revision='test'
        self.service=Workbench(Path(self.tmp.name),Path(self.tmp.name)/'resources',
                               native_reader=self.native,source_versions=self.src)

    def tearDown(self):
        self.service.close(); self.patch.stop(); self.tmp.cleanup()

    def test_refresh_reads_source_once_then_all_pages_reuse_snapshot(self):
        self.service.call('workbench_sync',{'view':'agents','page':1,'page_size':20})
        self.service.call('workbench_sync',{'view':'agents','page':2,'page_size':20})
        self.assertEqual(1,self.native.skills.call_count)
        self.service.call('workbench_sync',{'view':'agents','page':2,'page_size':20,'refresh':True})
        self.assertEqual(2,self.native.skills.call_count)

    def test_context_switch_does_not_reuse_snapshot(self):
        self.service.call('workbench_sync',{'view':'agents','page':1})
        self.src.context.return_value='other'
        self.service.call('workbench_sync',{'view':'agents','page':1})
        self.assertEqual(2,self.native.skills.call_count)


if __name__ == '__main__':
    unittest.main()
