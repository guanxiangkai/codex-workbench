"""账户管理工作台只公开账户、模型、配置和连接管理。"""
import unittest

from codex_workbench.catalog import DEFAULT_VIEW, MODULES, TOOLS, validate


class AccountManagementCatalogTests(unittest.TestCase):
    def test_only_account_management_modules_are_exposed(self):
        self.assertEqual(
            ['accounts', 'other_accounts', 'models', 'config', 'connections'],
            [module['id'] for module in MODULES],
        )
        self.assertEqual('accounts', DEFAULT_VIEW)
        self.assertEqual({'账户管理'}, {module['group'] for module in MODULES})

    def test_removed_work_record_and_knowledge_tools_are_rejected(self):
        exposed = {tool['name'] for tool in TOOLS}
        self.assertFalse(any(name.startswith(('planning_', 'library_', 'knowledge_', 'project_')) for name in exposed))
        for name in ('planning_create', 'knowledge_list', 'skill_detail', 'project_list', 'global_search'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate(name, {})
