"""知识中心只读取现有个人知识工具输出。"""
import json
import unittest
from subprocess import CompletedProcess

from codex_workbench.knowledge_catalog import KnowledgeCatalog


class KnowledgeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def run(command, **_kwargs):
            self.calls.append(command[1:])
            responses = {
                ('scopes',): [{"scope_key": "team", "display_name": "团队知识", "is_active": True}],
                ('catalog', 'team', '1000'): [{"scope_key": "team", "knowledge_key": "guide", "title": "使用指南", "summary": "已审核", "tags": ["入门"]}],
                ('lookup', 'team', 'guide'): [{"scope_key": "team", "knowledge_key": "guide", "revision": {"title": "使用指南", "content": "正文", "tags": ["入门"]}}],
            }
            return CompletedProcess(command, 0, '\n'.join(json.dumps(item) for item in responses[tuple(command[1:])]), '')
        self.catalog = KnowledgeCatalog(command='/fixture/personal-knowledge', run=run)

    def test_snapshot_and_detail_are_scoped_read_only_views(self):
        snapshot = self.catalog.snapshot()
        self.assertEqual([{'id': 'team', 'name': '团队知识', 'kind': None}], snapshot['scopes'])
        self.assertEqual('guide', snapshot['knowledge'][0]['knowledge_key'])
        detail = self.catalog.detail('team', 'guide')
        self.assertEqual('正文', detail['knowledge']['content'])
        self.assertTrue(all(call[0] in {'scopes', 'catalog', 'lookup'} for call in self.calls))

    def test_detail_rejects_unknown_scope_without_lookup(self):
        with self.assertRaisesRegex(ValueError, '范围不存在'):
            self.catalog.detail('unknown', 'guide')
        self.assertNotIn(('lookup', 'unknown', 'guide'), self.calls)


if __name__ == '__main__':
    unittest.main()
