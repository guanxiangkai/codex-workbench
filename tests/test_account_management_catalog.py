"""工作台只公开 1.0.0 的三个核心页面。"""
import unittest

from codex_workbench.catalog import DEFAULT_VIEW, MODULES, TOOLS, validate


class AccountManagementCatalogTests(unittest.TestCase):
    def test_only_confirmed_workbench_modules_are_exposed(self):
        self.assertEqual(
            ['accounts', 'other_accounts', 'models'],
            [module['id'] for module in MODULES],
        )
        self.assertEqual('accounts', DEFAULT_VIEW)
        self.assertEqual(['账户管理', '服务目录'], list(dict.fromkeys(module['group'] for module in MODULES)))

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
