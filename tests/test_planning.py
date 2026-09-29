"""Planning acceptance: explicit execution, real Runner hand-off, durable assets."""
import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.executor import Execution
from codex_workbench.planning import Planning
from codex_workbench.store import StoreError


class PlanningTests(unittest.TestCase):
    def setUp(self):
        clock = patch('codex_workbench.planning.local_today', return_value='2026-09-27')
        self.today = clock.start()
        self.addCleanup(clock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.requests = []
        self.entered, self.release = threading.Event(), threading.Event()
        outer = self
        class Executor:
            def execute(self, request, cancel, event):
                outer.requests.append(request)
                event('thread', '01234567-89ab-4cde-8123-0123456789ab')
                outer.entered.set()
                outer.release.wait(3)
                out = outer.plan.output_root / 'runs' / request.run_id
                (out / '图.png').write_bytes(b'image fixture')
                return Execution('review', '可查阅的结果')
        self.plan = Planning(Path(self.temp.name), Executor())
        self.addCleanup(self.plan.close)
        account = self.plan.store.create_execution_account('执行账户', self.temp.name)
        self.plan.store.record_account_subject(account['id'], 'synthetic-planning-subject')
        self.plan.store.set_default_execution_account(account['id'])

    def await_archived(self, task_id, count=1):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            detail = self.plan.detail(task_id)
            if len([c for c in detail['changes'] if c['kind']=='finished']) >= count:
                return detail
            time.sleep(.01)
        self.fail('归档未完成')

    def test_save_and_period_do_not_dispatch_and_stale_update_rejected(self):
        a = self.plan.create('甲', '提示', period='周一上午')
        b = self.plan.create('乙', '提示', period='周一上午')
        self.assertEqual(a['state'], 'ready')
        self.assertEqual(len(self.plan.snapshot()['tasks']), 2)
        self.assertEqual(self.requests, [])
        updated = self.plan.update(a['id'], a['version'], {'period':None})
        with self.assertRaises(StoreError) as err:
            self.plan.update(a['id'], a['version'], {'title':'过期编辑'})
        self.assertEqual(err.exception.code, 'version_conflict')
        self.assertIsNone(updated['period'])
        self.assertEqual(self.plan.detail(b['id'])['period'], '周一上午')

    def test_project_work_item_dates_are_persisted_and_strict(self):
        task = self.plan.create('月度拆分', project_name='项目甲', start_date='2026-09-01', due_date='2026-09-30')
        self.assertEqual((task['start_date'], task['due_date']), ('2026-09-01', '2026-09-30'))
        detail = self.plan.detail(task['id'])
        self.assertEqual((detail['start_date'], detail['due_date']), ('2026-09-01', '2026-09-30'))
        updated = self.plan.update(task['id'], task['version'], {'due_date': '2026-10-01'})
        self.assertEqual(updated['due_date'], '2026-10-01')
        for patch in ({'start_date': '2026-02-30'}, {'start_date': '2026-9-01'},
                      {'start_date': '2026-10-02', 'due_date': '2026-10-01'}):
            with self.assertRaises(StoreError) as err:
                self.plan.update(task['id'], updated['version'], patch)
            self.assertEqual(err.exception.code, 'validation')

    def test_past_schedule_is_read_only_across_mutations_and_upload_commit(self):
        task = self.plan.create('昨日工作项', start_date='2026-09-26', due_date='2026-09-27')
        asset = self.plan.upload('read.txt', base64.b64encode(b'keep').decode(), task_id=task['id'])
        upload = self.plan.upload_begin('pending.txt', 1, task_id=task['id'])
        self.plan.upload_chunk(upload['upload_id'], 0, 'QQ==')
        self.plan.knowledge_reader = lambda scope, key: self.fail('只读拒绝必须发生在读取知识前')
        self.today.return_value = '2026-09-28'
        self.assertTrue(self.plan.detail(task['id'])['read_only'])
        self.assertTrue(self.plan.snapshot()['tasks'][0]['read_only'])
        actions = [
            lambda: self.plan.update(task['id'], task['version'], {'due_date':'2026-10-01'}),
            lambda: self.plan.start(task['id'], task['version']),
            lambda: self.plan.remove(task['id'], task['version']),
            lambda: self.plan.followup(task['id'], task['version'], '更改说明'),
            lambda: self.plan.archive(task['id']),
            lambda: self.plan.upload('new.txt', 'QQ==', task_id=task['id']),
            lambda: self.plan.link(asset['id'], task['id']),
            lambda: self.plan.link_knowledge(task['id'], 'global', 'key'),
            lambda: self.plan.upload_begin('new.txt', 1, task_id=task['id']),
            lambda: self.plan.upload_commit(upload['upload_id']),
        ]
        for action in actions:
            with self.assertRaises(StoreError) as error:
                action()
            self.assertEqual(error.exception.code, 'read_only')
        current = self.plan.detail(task['id'])
        self.assertEqual(current['version'], task['version'])
        self.assertEqual(len(current['assets']), 1)
        self.assertEqual(self.plan.asset_content(asset['id'])['content_base64'], base64.b64encode(b'keep').decode())
        self.assertEqual(self.requests, [])

    def test_calendar_read_only_boundary_today_single_endpoint_and_unscheduled(self):
        for dates in ({}, {'start_date':'2026-09-27'}, {'due_date':'2026-09-27'},
                      {'start_date':'2026-09-01','due_date':'2026-09-30'}):
            task = self.plan.create('可编辑', **dates)
            self.assertFalse(task['read_only'])
            self.plan.update(task['id'], task['version'], {'title':'仍可编辑'})
        for dates in ({'start_date':'2026-09-26'}, {'due_date':'2026-09-26'},
                      {'start_date':'2026-09-01','due_date':'2026-09-26'}):
            with self.assertRaises(StoreError) as error:
                self.plan.create('历史排期', **dates)
            self.assertEqual(error.exception.code, 'read_only')
        task = self.plan.create('不能倒排到过去')
        with self.assertRaises(StoreError) as error:
            self.plan.update(task['id'], task['version'], {'due_date':'2026-09-26'})
        self.assertEqual(error.exception.code, 'read_only')

    def test_legacy_planning_row_dates_default_to_none(self):
        task = self.plan.create('兼容任务')
        self.assertIsNone(task['start_date'])
        self.assertIsNone(task['due_date'])
        self.assertIsNone(self.plan.snapshot()['tasks'][0]['start_date'])

    def test_real_runner_archives_files_and_followup_resumes_same_session(self):
        asset = self.plan.upload('输入.txt', base64.b64encode(b'input').decode())
        task = self.plan.create('执行任务', '处理资料', asset_ids=[asset['id']])
        run = self.plan.start(task['id'], task['version'])
        self.assertTrue(self.entered.wait(2))
        with self.assertRaises(StoreError):
            self.plan.start(task['id'], task['version'])
        self.release.set()
        detail = self.await_archived(task['id'])
        self.assertEqual(detail['status'], 'completed')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual({a['name'] for a in detail['assets']}, {'输入.txt','图.png','执行结果.md'})
        self.assertIn(asset['id'], self.requests[0].prompt)
        self.assertEqual(detail['runs'][0]['id'], run['run_id'])
        followup = self.plan.followup(task['id'], detail['version'], '进一步解释')
        self.assertEqual(len(self.requests), 1, '追问保存不能自动执行')
        self.plan.start(task['id'], followup['version'])
        again = self.await_archived(task['id'], 2)
        self.assertEqual(self.requests[1].resume_thread_id, detail['native_thread_id'])
        self.assertEqual(again['session_id'], detail['session_id'])
        self.assertEqual(len(again['runs']), 2)
        output = next(a for a in again['assets'] if a['name']=='图.png')
        self.assertEqual(len({r['run_id'] for r in output['refs']}), 2)
        self.plan.remove(task['id'], again['version'])
        self.assertEqual(self.plan.snapshot()['tasks'], [])
        self.assertEqual(len(self.plan.snapshot('library')['assets']), 3)
        self.assertEqual(base64.b64decode(self.plan.asset_content(output['id'])['content_base64']), b'image fixture')

    def test_stop_does_not_erase_material_or_claim_completion(self):
        task = self.plan.create('可停止')
        self.plan.start(task['id'], task['version'])
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.plan.stop(task['id'])['state'], 'cancelling')
        self.release.set()
        detail = self.await_archived(task['id'])
        self.assertEqual(detail['status'], 'stopped')
        self.assertEqual(detail['runs'][0]['result'], '')

    def test_chunked_upload_knowledge_reference_and_markdown_projection(self):
        task = self.plan.create('资料关联')
        self.assertEqual(task['note_status'], 'created')
        payload = b'first' * 70000
        upload = self.plan.upload_begin('附件.bin', len(payload), task_id=task['id'])
        first = base64.b64encode(payload[:262144]).decode()
        part = self.plan.upload_chunk(upload['upload_id'], 0, first)
        self.assertEqual(part['offset'], 262144)
        self.assertEqual(self.plan.upload_chunk(upload['upload_id'], 0, first), part)
        with self.assertRaises(StoreError):
            self.plan.upload_commit(upload['upload_id'])
        self.plan.upload_chunk(upload['upload_id'], 262144, base64.b64encode(payload[262144:]).decode())
        asset = self.plan.upload_commit(upload['upload_id'])
        self.assertEqual(asset['bytes'], len(payload))
        note_path = self.plan.output_root/'Tasks'/f"{task['id']}.md"
        self.assertIn('附件.bin', note_path.read_text())
        called = []
        def knowledge(scope, key):
            called.append((scope,key))
            return {'knowledge':{'title':'已审核知识','content':'只引用选中的内容'}}
        self.plan.knowledge_reader = knowledge
        linked = self.plan.link_knowledge(task['id'], 'existing', 'known')
        self.assertEqual(called, [('existing','known')])
        self.assertEqual(len(linked['knowledge_refs']), 1)
        self.assertEqual(self.requests, [])
        note = self.plan.export(task['id'])
        note_path = Path(note['path'])
        self.assertIn('附件.bin', note_path.read_text())
        note_path.write_text('用户自己的草稿')
        changed = self.plan.update(task['id'], linked['version'], {'title':'更新'})
        self.assertEqual(changed['note_status'], 'conflict')
        self.assertEqual(note_path.read_text(), '用户自己的草稿')

    def test_manual_export_reports_preserved_projection_conflict(self):
        task = self.plan.create('保留手写资料')
        asset = self.plan.upload('原始资料.txt', base64.b64encode(b'content').decode())
        self.plan.link(asset['id'], task['id'])
        asset_note = self.plan.output_root/'Assets'/f"{asset['id']}.md"
        self.assertIn('原始资料.txt', asset_note.read_text())
        asset_note.write_text('用户补充的说明')
        exported = self.plan.export(task['id'])
        self.assertEqual(exported['status'], 'conflict')
        self.assertEqual(self.plan.detail(task['id'])['note_status'], 'conflict')
        self.assertIn(str(asset_note), exported['conflicts'])
        self.assertEqual(asset_note.read_text(), '用户补充的说明')

    def test_failed_start_reuses_session_and_records_failure(self):
        from unittest.mock import patch
        task = self.plan.create('暂时无法启动')
        with patch.object(self.plan.runner, 'start', side_effect=ValueError('busy')):
            with self.assertRaises(ValueError):
                self.plan.start(task['id'], task['version'])
        detail = self.plan.detail(task['id'])
        self.assertEqual(detail['state'], 'ready')
        self.assertEqual(detail['runs'], [])
        self.assertEqual(detail['changes'][-1]['kind'], 'start_failed')
        session = detail['session_id']
        self.release.set()
        self.plan.start(task['id'], detail['version'])
        done = self.await_archived(task['id'])
        self.assertEqual(done['session_id'], session)

    def test_crashed_claim_before_prepare_is_recovered_without_dispatch(self):
        from unittest.mock import patch
        task = self.plan.create('被中断的任务')
        run = self.plan.store.claim(task['id'])
        with self.plan.db:
            self.plan.db.execute('INSERT INTO planning_owners VALUES(?,?)', (task['id'],999999))
        with patch('codex_workbench.planning.os.kill', side_effect=ProcessLookupError):
            self.plan._recover()
        detail = self.plan.detail(task['id'])
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(detail['runs'][0]['id'], run['id'])
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
