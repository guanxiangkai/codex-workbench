"""工作台只公开 1.0.0 的三个核心页面。"""
import unittest
import json
from pathlib import Path

from codex_workbench.catalog import DEFAULT_VIEW, MODULES, TOOLS, validate


class AccountManagementCatalogTests(unittest.TestCase):
    def test_only_confirmed_workbench_modules_are_exposed(self):
        self.assertEqual(
            ['accounts', 'other_accounts', 'models'],
            [module['id'] for module in MODULES],
        )
        self.assertEqual(['Codex 账户', '其他账户', '模型目录'], [module['name'] for module in MODULES])
        self.assertEqual('accounts', DEFAULT_VIEW)
        self.assertEqual(['账户与模型'], list(dict.fromkeys(module['group'] for module in MODULES)))

    def test_retired_page_views_are_rejected(self):
        for view in ('config', 'knowledge'):
            with self.subTest(view=view):
                with self.assertRaises(ValueError):
                    validate('workbench_state', {'view': view})

    def test_work_records_planning_and_standalone_connections_are_not_modules(self):
        exposed = {tool['name'] for tool in TOOLS}
        self.assertNotIn('skill_detail', exposed)
        self.assertIn('knowledge_list', exposed)
        self.assertIn('knowledge_detail', exposed)
        self.assertNotIn('connections', [module['id'] for module in MODULES])
        self.assertFalse(any(name.startswith(('planning_', 'library_', 'project_')) for name in exposed))
        for name in ('planning_create', 'project_list', 'global_search'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate(name, {})

    def test_manual_profile_update_is_app_only(self):
        for name in ('account_profile_update', 'account_profile_capture'):
            tool = next(item for item in TOOLS if item['name'] == name)
            self.assertEqual(['app'], tool['_meta']['ui']['visibility'])
        with self.assertRaises(ValueError):
            validate('account_profile_import', {'view':'accounts','id':'current','display_name':'Name'})

    def test_knowledge_detail_is_model_visible_and_native_contract_is_current(self):
        detail = next(item for item in TOOLS if item['name'] == 'knowledge_detail')
        self.assertNotIn('visibility', detail.get('_meta', {}).get('ui', {}))
        contract = json.loads((Path(__file__).parents[1] / 'native-entry-contract.json').read_text())
        self.assertEqual(['accounts', 'other_accounts', 'models'], [item['id'] for item in contract['modules']])
        self.assertEqual(MODULES, contract['modules'])
        self.assertIn('knowledge_list', contract['tools'])
        self.assertNotIn('skill_detail', contract['tools'])
        self.assertEqual('ui://codex-workbench/v8/workbench.html', contract['resource_uri'])
