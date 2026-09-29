import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.planning import Planning
from codex_workbench.planning_intake import PlanningIntake
from codex_workbench.executor import Execution
from codex_workbench.store import StoreError


class PlanningIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.today = patch('codex_workbench.planning.local_today', return_value='2026-09-28')
        self.today.start(); self.addCleanup(self.today.stop)
        self.requests = []
        outer = self
        class Executor:
            def execute(inner, request, cancel, event):
                outer.requests.append(request)
                (Path(request.cwd) / '.codex-workbench' / 'outputs' / request.run_id).mkdir(parents=True, exist_ok=True)
                return Execution('review', '已回答')
        self.plan = Planning(Path(self.tmp.name), Executor())
        self.addCleanup(self.plan.close)
        self.account = self.plan.store.create_execution_account('账户', self.tmp.name)
        self.plan.store.set_default_execution_account(self.account['id'])
        self.project = self.plan.store.create_project('项目', self.tmp.name)

    def item(self, **overrides):
        value = {'intent': 'create_task', 'title': '新任务', 'prompt': '整理资料',
                 'project_id': self.project['id'], 'tags': []}
        value.update(overrides)
        return value

    def test_save_is_atomic_idempotent_and_does_not_execute(self):
        result = self.plan.intake_save('op-1', [self.item(), self.item(title='第二项')])
        self.assertEqual(2, len(result['tasks']))
        self.assertEqual([], self.plan.store.list_runs(result['tasks'][0]['id']))
        again = self.plan.intake_save('op-1', [self.item(), self.item(title='第二项')])
        self.assertEqual(result, again)
        with self.assertRaises(StoreError) as error:
            self.plan.intake_save('op-1', [self.item(title='不同内容')])
        self.assertEqual('conflict', error.exception.code)
        with self.assertRaises(StoreError):
            self.plan.intake_save('op-2', [self.item(), self.item(project_id='missing')])
        self.assertEqual(2, len(self.plan.snapshot()['tasks']))

    def test_batch_messages_append_without_prompt_overwrite_or_internal_version_conflict(self):
        task = self.plan.create('已有任务', '原始说明', project_id=self.project['id'], execution_account_id=self.account['id'])
        items = [self.item(intent='supplement', target_task_id=task['id'], expected_version=task['version'], prompt='补充 A'),
                 self.item(intent='question', target_task_id=task['id'], expected_version=task['version'], prompt='问题 B')]
        saved = self.plan.intake_save('op-messages', items)
        detail = saved['tasks'][0]
        self.assertEqual('原始说明', detail['prompt'])
        self.assertEqual(2, len(detail['messages']))
        self.assertEqual(task['version'] + 2, detail['version'])
        with self.assertRaises(StoreError) as error:
            self.plan.intake_save('stale', [self.item(intent='question', target_task_id=task['id'], expected_version=task['version'], prompt='过期')])
        self.assertEqual('version_conflict', error.exception.code)

    def test_cross_project_section_and_past_dates_are_rejected(self):
        section = self.plan.store.create_section('其他分区')
        with self.assertRaises(StoreError) as error:
            self.plan.intake_save('wrong-section', [self.item(section_id=section['id'])])
        self.assertEqual('validation', error.exception.code)
        with self.assertRaises(StoreError) as error:
            self.plan.intake_save('past', [self.item(start_date='2026-09-01', due_date='2026-09-02')])
        self.assertEqual('read_only', error.exception.code)

    def test_confirmed_project_and_section_names_create_atomically(self):
        item = self.plan.intake('整理新项目资料', models_provider=lambda: [])['items'][0]
        item.update(project_id=None, project_name='新项目', section_id=None, section_name='新分区')
        saved = self.plan.intake_save('new-project', [item])
        task = saved['tasks'][0]
        project = self.plan.store.get_project(task['project_id'])
        self.assertEqual('新项目', project['name'])
        self.assertEqual('新分区', next(section for section in self.plan.store.list_sections()
                                     if section['id'] == project['section_id'])['name'])
        with self.assertRaises(StoreError):
            self.plan.intake_save('new-project-failed', [
                self.item(project_id=None, project_name='不应保留项目', section_name='不应保留分区'),
                self.item(project_id=None, project_name=None, section_name='无效分区'),
            ])
        self.assertNotIn('不应保留项目', {project['name'] for project in self.plan.store.list_projects()})
        self.assertNotIn('不应保留分区', {section['name'] for section in self.plan.store.list_sections()})

    def test_question_run_uses_task_context_and_keeps_message_association(self):
        task = self.plan.create('已有任务', '原始说明', project_id=self.project['id'], execution_account_id=self.account['id'])
        saved = self.plan.intake_save('op-question', [
            self.item(intent='supplement', target_task_id=task['id'], expected_version=task['version'], prompt='补充上下文'),
            self.item(intent='question', target_task_id=task['id'], expected_version=task['version'], prompt='请回答这个问题'),
        ])
        detail = saved['tasks'][0]
        message = next(item for item in detail['messages'] if item['kind'] == 'question')
        started = self.plan.start(task['id'], detail['version'], message['id'])
        self.assertEqual(message['id'], started['message_id'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.plan.detail(task['id'])
            message = next(item for item in detail['messages'] if item['id'] == message['id'])
            if message['state'] == 'completed':
                break
            time.sleep(.01)
        else:
            self.fail('question run did not finish')
        self.assertEqual('原始说明', detail['prompt'])
        self.assertEqual(1, len(message['runs']))
        self.assertEqual(message['id'], message['runs'][0]['message_id'])
        self.assertIn('原始说明', self.requests[-1].prompt)
        self.assertIn('补充上下文', self.requests[-1].prompt)
        self.assertIn('请回答这个问题', self.requests[-1].prompt)

    def test_explicit_current_task_pins_the_task_project(self):
        other_path = Path(self.tmp.name) / 'other'; other_path.mkdir()
        other = self.plan.store.create_project('其他项目', str(other_path))
        task = self.plan.create('已有任务', '原始说明', project_id=self.project['id'], execution_account_id=self.account['id'])
        response = {'items': [{'intent': 'question', 'target_task_id': task['id'],
                    'expected_version': task['version'], 'title': '答复', 'prompt': '请继续',
                    'project_id': self.project['id'], 'tags': []}]}
        router = type('Router', (), {'call': lambda _, *args, **kwargs: {'text': json.dumps(response)}})()
        intake = PlanningIntake(lambda: [], self.plan.store.list_projects, self.plan.store.list_sections,
                                self.plan._intake_tasks, router=router)
        item = intake.generate('请继续', task_id=task['id'], project_id=self.project['id'])['items'][0]
        self.assertEqual(task['id'], item['target_task_id'])
        self.assertEqual(self.project['id'], item['project_id'])
        self.assertNotEqual(other['id'], item['project_id'])

    def test_current_task_project_conflict_and_ambiguous_short_reply_remain_unassigned(self):
        other_path = Path(self.tmp.name) / 'other'; other_path.mkdir()
        other = self.plan.store.create_project('其他项目', str(other_path))
        task = self.plan.create('已有任务', '原始说明', project_id=self.project['id'], execution_account_id=self.account['id'])
        called = []
        router = type('Router', (), {'call': lambda _, *args, **kwargs: called.append(True)})()
        intake = PlanningIntake(lambda: [], self.plan.store.list_projects, self.plan.store.list_sections,
                                self.plan._intake_tasks, router=router)
        mismatch = intake.generate('继续', task_id=task['id'], project_id=other['id'])
        uncertain = intake.generate('继续')
        self.assertEqual('current_task_project_mismatch', mismatch['warning'])
        self.assertIsNone(mismatch['items'][0]['project_id'])
        self.assertEqual('assignment_uncertain', uncertain['warning'])
        self.assertIsNone(uncertain['items'][0]['project_id'])
        self.assertEqual([], called)


if __name__ == '__main__':
    unittest.main()
