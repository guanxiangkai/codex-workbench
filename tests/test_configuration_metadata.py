import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.configuration_center import configuration_entries
from codex_workbench.configuration_metadata import read_organization
from codex_workbench.readonly_sources import ReadonlyCredentials
from codex_workbench.service import _page_view
from codex_workbench.source_versions import SourceVersions


class ConfigurationMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'workbench-settings.json'
        self.db = self.root / 'workbench.sqlite3'

    def save(self, entries, folders=()):
        self.path.write_text(json.dumps({'credential_organization': {'entries': entries, 'folders': list(folders)}}))

    def catalog(self):
        catalog = ReadonlyCredentials(self.db, organization_path=self.path)
        catalog._snapshot = lambda: {'entries': [{'entry_id': 'current', 'revision': 3}], 'status': {}}
        return catalog

    def test_exact_current_ids_only_and_never_restore_structure_or_secrets(self):
        self.save([{'entry_id': 'old', 'label': '历史账户'},
                   {'entry_id': 'current', 'label': '测试账户', 'description': '用于测试平台登录。',
                    'display_type': 'account', 'tags': ['测试'], 'folder_id': 'accounts',
                    'kind': 'private_key', 'has_fields': ['private_key'], 'password': 'not-for-ui'}],
                  [{'id': 'accounts', 'name': '账户', 'private_key': 'not-for-ui'}])
        before = self.path.read_bytes()
        result = self.catalog().list()
        self.assertEqual(['current'], [e['id'] for e in result['entries']])
        entry = result['entries'][0]
        self.assertEqual('测试账户', entry['label'])
        self.assertEqual('用于测试平台登录。', entry['description'])
        self.assertEqual('unknown', entry['kind'])
        self.assertEqual([], entry['has_fields'])
        self.assertNotIn('not-for-ui', json.dumps(result))
        self.assertFalse(self.db.exists())
        self.assertEqual(before, self.path.read_bytes())
        projected = configuration_entries(result['entries'], [])
        page = _page_view({'entries': projected, 'folders': result['folders']}, kind='account', query='测试平台')
        self.assertEqual(1, len(page['entries']))
        self.assertEqual('账户登录', page['facets']['kinds'][0]['name'])

    def test_local_registration_wins_and_cleared_folder_is_not_restored(self):
        self.save([{'entry_id': 'current', 'label': '持久名称', 'description': '用途说明',
                    'folder_id': 'old', 'tags': ['旧标签']}])
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE credential_organizer(entry_id TEXT, label TEXT, folder_id TEXT, tags_json TEXT)')
            db.execute('INSERT INTO credential_organizer VALUES (?, ?, NULL, ?)', ('current', '本机名称', '[]'))
        before = self.db.read_bytes()
        entry = self.catalog().list()['entries'][0]
        self.assertEqual('本机名称', entry['label'])
        self.assertEqual('用途说明', entry['description'])
        self.assertIsNone(entry['folder_id'])
        self.assertEqual([], entry['tags'])
        self.assertEqual(before, self.db.read_bytes())

    def test_missing_corrupt_and_duplicate_metadata(self):
        self.assertEqual({'entries': {}, 'folders': []}, read_organization(self.path))
        for raw in ('{', '[]', '{"credential_organization":null}'):
            self.path.write_text(raw)
            with self.assertRaisesRegex(ValueError, '配置展示信息格式无效'):
                read_organization(self.path)
        self.save([{'entry_id': 'current'}, {'entry_id': 'current'}])
        with self.assertRaises(ValueError):
            read_organization(self.path)

    def test_metadata_change_invalidates_configuration_source(self):
        with patch('codex_workbench.source_versions.current_account_home', return_value=str(self.root)):
            versions = SourceVersions(self.db, self.root, self.root)
            before = versions.signature('config')
            self.save([{'entry_id': 'current', 'label': '新名称'}])
            self.assertNotEqual(before, versions.signature('config'))
