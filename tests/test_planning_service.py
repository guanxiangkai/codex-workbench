"""计划服务在本机数据路径上的工具接线契约。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from codex_workbench.catalog import BY_NAME, validate
from readonly_fixture import Fixture as ReadonlyFixture


class FakePlanning:
    def __init__(self):
        self.calls=[]
        self.closed=False

    def snapshot(self, *, view):
        self.calls.append(('snapshot', view))
        return {'tasks': [], 'projects': [], 'sections': [], 'accounts': []} if view=='planning' else {'assets': []}

    def create(self, **args): return self._item('create', **args)
    def update(self, **args): return self._item('update', **args)
    def start(self, **args): return self._item('start', **args)
    def stop(self, **args): return self._item('stop', **args)
    def archive(self, **args): return self._item('archive', **args)
    def remove(self, **args): return self._item('remove', **args)
    def followup(self, **args): return self._item('followup', **args)
    def intake(self, text, project_id=None, task_id=None, start_date=None, due_date=None, period=None, models_provider=None):
        self.calls.append(('intake', {'text': text, 'project_id': project_id, 'task_id': task_id,
                                     'start_date': start_date, 'due_date': due_date, 'period': period,
                                     'models_provider': models_provider}))
        return {'source': 'manual', 'warning': 'model_failed', 'items': []}
    def intake_save(self, operation_id, items):
        self.calls.append(('intake_save', {'operation_id': operation_id, 'items': items}))
        return {'items': [], 'tasks': []}
    def link_knowledge(self, **args): return self._item('knowledge_link', **args)
    def export(self, **args): return self._item('export', **args)
    def detail(self, **args): return self._item('detail', **args)
    def upload(self, **args): return self._item('upload', **args)
    def upload_begin(self, **args): return self._item('upload_begin', **args)
    def upload_chunk(self, **args): return self._item('upload_chunk', **args)
    def upload_commit(self, **args): return self._item('upload_commit', **args)
    def link(self, **args): return self._item('link', **args)
    def library_list(self, **args): return self._item('list', **args)
    def asset_content(self, **args): return self._item('content', **args)

    def _item(self, kind, **args):
        self.calls.append((kind, args))
        return {'kind':kind, **args}

    def close(self): self.closed=True


class PlanningServiceTest(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.planning=FakePlanning();self.board._planning=self.planning
        self.addCleanup(self.fixture.close)

    def test_local_views_bypass_snapshot_sources_and_sync_by_local_revision(self):
        planning=self.board.call('workbench_state', {'view':'planning'})
        library=self.board.call('library_list', {})['item']
        synced=self.board.call('workbench_sync', {'view':'planning'})
        self.assertFalse(planning['read_only'])
        self.assertEqual([], planning['tasks'])
        self.assertEqual('list', library['kind'])
        self.assertEqual('local-planning', synced['context'])
        self.assertEqual([], self.fixture.native.calls)
        self.assertEqual([('snapshot','planning'),('list',{}),('snapshot','planning')], self.planning.calls)

    def test_business_tools_are_wrapped_and_write_tools_are_declared(self):
        created=self.board.call('planning_create', {'title':'整理验收'})
        followed=self.board.call('planning_followup', {'task_id':'task_1','expected_version':1,'prompt':'补充证据'})
        knowledge=self.board.call('planning_knowledge_link', {'task_id':'task_1','scope':'global','key':'style-guide'})
        exported=self.board.call('planning_export', {'task_id':'task_1'})
        archived=self.board.call('planning_archive', {'task_id':'task_1'})
        begun=self.board.call('library_upload_begin', {'name':'evidence.txt','size':3})
        chunk=self.board.call('library_upload_chunk', {'upload_id':'upload_1','offset':0,'content_base64':'YWJj'})
        committed=self.board.call('library_upload_commit', {'upload_id':'upload_1'})
        listed=self.board.call('library_list', {})
        content=self.board.call('library_content', {'asset_id':'asset_1','offset':0,'length':16})
        self.assertEqual('create', created['item']['kind'])
        self.assertEqual('followup', followed['item']['kind'])
        self.assertEqual('knowledge_link', knowledge['item']['kind'])
        self.assertEqual('export', exported['item']['kind'])
        self.assertEqual('archive', archived['item']['kind'])
        self.assertEqual('upload_begin', begun['item']['kind'])
        self.assertEqual('upload_chunk', chunk['item']['kind'])
        self.assertEqual('upload_commit', committed['item']['kind'])
        self.assertEqual('list', listed['item']['kind'])
        self.assertEqual('content', content['item']['kind'])
        self.assertFalse(BY_NAME['planning_create']['annotations']['readOnlyHint'])
        self.assertFalse(BY_NAME['planning_create']['annotations']['idempotentHint'])
        self.assertTrue(BY_NAME['planning_archive']['annotations']['idempotentHint'])
        self.assertTrue(BY_NAME['library_content']['annotations']['readOnlyHint'])
        with self.assertRaises(ValueError):
            validate('library_upload', {'name':'x.txt','content_base64':'a'*(349528+1)})
        with self.assertRaises(ValueError):
            validate('library_upload_chunk', {'upload_id':'upload_1','offset':0,'content_base64':'a'*(349528+1)})
        for patch in ({'state':'running'},{'project_id':'project_1'},{'asset_ids':['asset_1']}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                validate('planning_update', {'task_id':'task_1','expected_version':1,'patch':patch})

    def test_delivery_catalog_accepts_the_complete_save_card_payload(self):
        arguments = {
            'task_id': 'task_1', 'expected_version': 3, 'action': 'save_card',
            'payload': {'card': {
                'goal': '完成验收', 'scope': [], 'preserve': [],
                'acceptance': [{'id': 'A1', 'text': '保存任务卡'}],
                'facts': [], 'assumptions': [], 'original': '完成验收',
            }},
        }
        self.assertEqual(arguments, validate('planning_delivery', arguments))

    def test_close_closes_an_initialized_local_planning_service(self):
        self.board.close()
        self.assertTrue(self.planning.closed)

    def test_text_intake_uses_catalog_text_and_save_returns_top_level_payload(self):
        draft = self.board.call('planning_intake', {'text': '明天整理验收', 'project_id': 'project_1', 'period': '上午'})
        saved = self.board.call('planning_intake_save', {'operation_id': 'intake-op', 'items': [{
            'intent': 'create_task', 'title': '验收', 'prompt': '整理验收', 'tags': []
        }]})
        self.assertEqual('manual', draft['draft']['source'])
        self.assertEqual({'items': [], 'tasks': []}, saved)
        name, args = self.planning.calls[-2]
        self.assertEqual('intake', name)
        self.assertEqual('明天整理验收', args['text'])
        self.assertEqual('project_1', args['project_id'])
        self.assertEqual('上午', args['period'])
        self.assertEqual('intake_save', self.planning.calls[-1][0])

    def test_real_service_preserves_assets_and_syncs_only_business_changes(self):
        self.board._planning = None
        with patch('codex_workbench.service.timestamp', return_value='first-observation'):
            initial = self.board.call('workbench_sync', {'view':'planning'})
        with patch('codex_workbench.service.timestamp', return_value='later-observation'):
            same = self.board.call('workbench_sync', {'view':'planning', 'revision':initial['revision']})
        self.assertTrue(same['unchanged'])
        asset = self.board.call('library_upload', {'name':'资料.txt', 'content_base64':'YWJj'})['item']
        task = self.board.call('planning_create', {'title':'整理资料', 'asset_ids':[asset['id']]})['item']
        changed = self.board.call('workbench_sync', {'view':'planning', 'revision':initial['revision']})
        self.assertFalse(changed['unchanged'])
        self.assertEqual(task['id'], changed['data']['tasks'][0]['id'])
        self.assertEqual([], self.board.call('planning_detail', {'task_id':task['id']})['item']['runs'])
        self.board.call('planning_delete', {'task_id':task['id'], 'expected_version':task['version']})
        library = self.board.call('library_list', {})['item']
        self.assertEqual(1, library['total'])
        task_ref = next(ref for ref in library['assets'][0]['refs'] if ref['task_id'] == task['id'])
        self.assertEqual('整理资料', task_ref['task_title'])
        self.assertTrue(task_ref['task_deleted'])
        self.assertIsNotNone(task_ref['task_deleted_at'])
        by_task = self.board.call('library_list', {'query':'整理', 'limit':1})['item']
        self.assertEqual(1, by_task['total'])
        self.assertEqual(asset['id'], by_task['assets'][0]['id'])
        by_file = self.board.call('library_list', {'query':'资料.txt', 'offset':1})['item']
        self.assertEqual(1, by_file['total'])
        self.assertEqual([], by_file['assets'])
        self.assertEqual(0, self.board.call('library_list', {'query':'%'})['item']['total'])
        with self.assertRaises(ValueError):
            validate('library_list', {'query':'x'*301})
        self.assertEqual('YWJj', self.board.call('library_content', {'asset_id':asset['id']})['item']['content_base64'])
        self.assertEqual([], self.fixture.native.calls)

    def test_local_page_bootstrap_and_configured_executor(self):
        from codex_workbench.ui_release import UiRelease
        self.board._planning = None
        self.board.codex = '/Applications/ControlledCodex/codex'
        self.board.ui_release = UiRelease(source_mode=True)
        for view in ('planning',):
            page = self.board.page(view)
            self.assertIn("const INITIAL_PAGE='"+view+"';",page['html'])
            self.assertNotIn('const WORKBENCH_BOOTSTRAP=null;',page['html'])
        with self.assertRaises(ValueError):
            self.board.page('library')
        self.assertEqual(('/Applications/ControlledCodex/codex',), self.board._planning.runner.executor.command)
        self.assertEqual([], self.fixture.native.calls)


if __name__=='__main__':
    unittest.main()
