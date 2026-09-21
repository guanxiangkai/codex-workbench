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

    def test_resource_fragments_add_modify_and_delete_change_source_revision(self):
        with tempfile.TemporaryDirectory() as temporary, patch('codex_workbench.source_versions.current_account_home', return_value=temporary):
            root=Path(temporary);resources=root/'resources'
            versions=SourceVersions(root/'workbench.sqlite3',root,resources)
            for directory, view in (('models','models'),('accounts','other_accounts')):
                path=resources/directory/'entry.json';path.parent.mkdir(parents=True)
                before=versions.signature(view)
                path.write_text('{"version":1}',encoding='utf-8')
                added=versions.signature(view)
                path.write_text('{"version":2}',encoding='utf-8')
                changed=versions.signature(view)
                path.unlink()
                removed=versions.signature(view)
                self.assertNotEqual(before,added)
                self.assertNotEqual(added,changed)
                self.assertNotEqual(changed,removed)


class FilterShapeTest(unittest.TestCase):
    def test_realistic_model_shape_returns_all_matching_items(self):
        rows=[{'id':f'm{i}','name':f'MiniMax {i}','model_type':'chat',
               'provider_id':'minimax','provider_name':'MiniMax',
               'tags':['minmax'] if i % 2 == 0 else []} for i in range(25)]
        result=_page_view({'models':rows},query='minmax')
        self.assertEqual(25,len(result['models']))
        self.assertNotIn('pagination',result)

    def test_accounts_do_not_leak_full_provider_lists(self):
        rows=[{'id':str(i),'provider_id':'glm','is_current':i==24} for i in range(25)]
        result=_page_view({'accounts':rows,'providers':[{'id':'glm','name':'智谱','accounts':rows}]})
        self.assertEqual(25,len(result['accounts']))
        self.assertEqual('24',result['accounts'][0]['id'])
        self.assertNotIn('accounts',result['providers'][0])

    def test_empty_shape_has_no_pagination(self):
        self.assertNotIn('pagination',_page_view({'models':[]}))

    def test_filter_aliases_and_array_tags(self):
        value={'entries':[{'id':'a','kind':'service','service_type':'chat','service_type_label':'对话','account_provider_ids':['glm'],'tags':['production']}]}
        self.assertEqual(1,len(_page_view(value,kind='chat')['entries']))
        self.assertEqual(1,len(_page_view(value,provider='glm',tag='production')['entries']))

    def test_folders_can_be_counted_from_real_tree_shape(self):
        rows=[{'id':'a','folder_id':'grandchild'},
              {'id':'b','folder_id':'root','folder_parent_id':None}]
        result=_page_view({'entries':rows,'folders':[{'id':'root','parent_id':None},{'id':'child','parent_id':'root'},{'id':'grandchild','parent_id':'child'}]},folder='root')
        self.assertEqual(2,len(result['entries']))
        self.assertEqual(2,result['facets']['folder_counts']['root'])
        self.assertEqual(1,result['facets']['folder_counts']['child'])


class SnapshotFilterTest(unittest.TestCase):
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

    def test_sync_reads_only_persisted_snapshot(self):
        pending=self.service.call('workbench_sync',{'view':'agents'})
        self.assertEqual('pending',pending['data']['status']['snapshot']['state'])
        self.assertEqual(0,self.native.skills.call_count)
        self.service.collect_snapshot('agents')
        self.service.call('workbench_sync',{'view':'agents'})
        self.service.call('workbench_sync',{'view':'agents','query':'2'})
        self.assertEqual(1,self.native.skills.call_count)

    def test_context_switch_does_not_reuse_snapshot(self):
        self.service.collect_snapshot('agents')
        self.service.call('workbench_sync',{'view':'agents'})
        self.src.context.return_value='other'
        result=self.service.call('workbench_sync',{'view':'agents'})
        self.assertEqual('pending',result['data']['status']['snapshot']['state'])
        self.assertEqual(1,self.native.skills.call_count)


if __name__ == '__main__':
    unittest.main()
