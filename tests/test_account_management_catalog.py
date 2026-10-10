"""工作台只公开两个已确认分区。"""
import unittest

from codex_workbench.catalog import DEFAULT_VIEW, MODULES, TOOLS, validate


class AccountManagementCatalogTests(unittest.TestCase):
    def test_only_confirmed_workbench_modules_are_exposed(self):
        self.assertEqual(
            ['accounts', 'other_accounts', 'config', 'knowledge', 'models'],
            [module['id'] for module in MODULES],
        )
        self.assertEqual('accounts', DEFAULT_VIEW)
        self.assertEqual(['账户与配置', '能力与知识'], list(dict.fromkeys(module['group'] for module in MODULES)))

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
        tool = next(item for item in TOOLS if item['name'] == 'account_profile_update')
        self.assertEqual(['app'], tool['_meta']['ui']['visibility'])
