import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_workbench.service import Workbench
from codex_workbench.snapshot_store import SnapshotStore


class SnapshotStoreTest(unittest.TestCase):
    def test_restart_reuses_last_successful_public_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch('codex_workbench.service.UiRelease') as release:
            release.return_value.revision='test'
            native=Mock();native.skills=Mock(return_value=[{'id':'one','display_name':'One'}])
            versions=Mock();versions.context=Mock(return_value='account-a')
            first=Workbench(Path(directory),Path(directory)/'resources',native_reader=native,source_versions=versions)
            self.assertTrue(first.collect_snapshot('agents'))
            first.close()
            second=Workbench(Path(directory),Path(directory)/'resources',native_reader=native,source_versions=versions)
            data=second.sync('agents')['data']
            self.assertEqual('ready',data['status']['snapshot']['state'])
            self.assertEqual(['one'],[item['id'] for item in data['skills']])
            self.assertEqual(1,native.skills.call_count)
            second.close()

    def test_failed_collection_keeps_last_successful_data(self):
        with tempfile.TemporaryDirectory() as directory, patch('codex_workbench.service.UiRelease') as release:
            release.return_value.revision='test'
            native=Mock();native.skills=Mock(return_value=[{'id':'one','display_name':'One'}])
            versions=Mock();versions.context=Mock(return_value='account-a')
            board=Workbench(Path(directory),Path(directory)/'resources',native_reader=native,source_versions=versions)
            self.assertTrue(board.collect_snapshot('agents'))
            native.skills.side_effect=ValueError('private source failure')
            self.assertFalse(board.collect_snapshot('agents'))
            data=board.sync('agents')['data']
            self.assertEqual('stale',data['status']['snapshot']['state'])
            self.assertEqual(['one'],[item['id'] for item in data['skills']])
            self.assertNotIn('private',str(data))
            board.close()

    def test_write_failure_does_not_replace_memory_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SnapshotStore(Path(directory))
            store.put('a','models',{'models':[{'id':'old'}]})
            with patch.object(store,'_save',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):store.put('a','models',{'models':[{'id':'new'}]})
            self.assertEqual('old',store.get('a','models')['data']['models'][0]['id'])

    def test_load_discards_malformed_view_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'snapshots.json'
            path.write_text('{"version":1,"contexts":{"a":{"models":{"data":{},"updated_at":"now"},"unknown":{"data":{},"updated_at":"now"},"agents":{"data":[],"updated_at":"now"},"config":{}}}}')
            store=SnapshotStore(Path(directory))
            self.assertEqual(['models'],list(store.entries['a']))

    def test_account_refresh_keeps_confirmed_profile_and_usage_when_source_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch('codex_workbench.service.UiRelease') as release:
            release.return_value.revision='test'
            versions=Mock();versions.context=Mock(return_value='account-a')
            board=Workbench(Path(directory),Path(directory)/'resources',native_reader=Mock(),source_versions=versions)
            old={'accounts':[{'id':'current','email':'user@example.invalid','name':'官网姓名','name_source':'official_page',
                              'avatar_data_uri':'data:image/png;base64,YQ==','profile_observed_at':'2026-01-01T00:00:00+00:00',
                              'remaining_percent':80,'reset_cards':[{'id':'weekly'}]}]}
            refreshed={'accounts':[{'id':'current','email':'user@example.invalid','name':'用户名未提供','name_source':'unavailable',
                                    'remaining_percent':None,'reset_cards':None}]}
            result=board._merge_account_fields('accounts',refreshed,old)['accounts'][0]
            self.assertEqual('官网姓名',result['name'])
            self.assertEqual('official_page',result['name_source'])
            self.assertEqual(80,result['remaining_percent'])
            self.assertEqual('data:image/png;base64,YQ==',result['avatar_data_uri'])
            board.close()


if __name__ == '__main__':
    unittest.main()
