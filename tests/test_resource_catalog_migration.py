import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import migrate_resource_catalogs as migration
from codex_workbench.model_catalog import configured_models
from codex_workbench.other_accounts import other_accounts


class ResourceCatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.resources=Path(self.temporary.name)/'resources'
        model={'id':'../unsafe-id','name':'Model','service_name':'Service','model_type':'reasoning','base_url':'https://example.invalid/v1','model_source':'explicit','model':'model','updated_at':'2026-09-21T00:00:00+08:00'}
        account={'id':'account','label':'Account'}
        for name, data in (('models',{'version':1,'models':[model]}),('accounts',{'version':1,'providers':[{'id':'provider','name':'Provider','accounts':[account]}]})):
            path=self.resources/name/'catalog.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(data),encoding='utf-8')

    def test_apply_uses_safe_names_preserves_legacy_and_keeps_projection(self):
        before_models=configured_models(self.resources/'models/catalog.json');before_accounts=other_accounts(self.resources/'accounts/catalog.json')
        plans=tuple(migration._plan(self.resources,name) for name in ('models','accounts'))
        migration.apply(self.resources,plans)
        self.assertEqual(before_models,configured_models(self.resources/'models/catalog.json'))
        self.assertEqual(before_accounts,other_accounts(self.resources/'accounts/catalog.json'))
        self.assertTrue((self.resources/'models/legacy/catalog.json').is_file())
        names=[path.name for path in (self.resources/'models').glob('*.json')]
        self.assertEqual(1,len(names));self.assertNotIn('/',names[0]);self.assertNotIn('..',names[0])

    def test_failed_catalog_move_restores_both_catalogs_and_removes_new_fragments(self):
        plans=tuple(migration._plan(self.resources,name) for name in ('models','accounts'))
        original=os.replace
        def fail_accounts(source,target):
            if str(target).endswith('/accounts/legacy/catalog.json'):
                raise OSError('synthetic move failure')
            return original(source,target)
        with patch('migrate_resource_catalogs.os.replace',side_effect=fail_accounts),self.assertRaises(OSError):
            migration.apply(self.resources,plans)
        for name in ('models','accounts'):
            directory=self.resources/name
            self.assertTrue((directory/'catalog.json').is_file())
            self.assertEqual(['catalog.json'],[path.name for path in directory.glob('*.json')])
            self.assertFalse((directory/'legacy').exists())

