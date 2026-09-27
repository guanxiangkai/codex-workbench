"""Planning board run-history projection acceptance."""
import tempfile
import time
import unittest
from pathlib import Path

from codex_workbench.executor import Execution
from codex_workbench.planning import Planning


class PlanningStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        class Executor:
            def execute(self, request, cancel, event):
                event('thread', '01234567-89ab-4cde-8123-0123456789ab')
                return Execution('review', '公开结果')

        self.plan = Planning(Path(self.temp.name), Executor())
        self.addCleanup(self.plan.close)
        account = self.plan.store.create_execution_account('执行账户', self.temp.name)
        self.plan.store.record_account_subject(account['id'], 'synthetic-statistics-subject')
        self.plan.store.set_default_execution_account(account['id'])

    def _await_runs(self, task_id, count):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            detail = self.plan.detail(task_id)
            if len(detail['runs']) == count and detail['state'] == 'done':
                return detail
            time.sleep(.01)
        self.fail('运行未完成')

    def test_snapshot_projects_all_runs_without_leaking_or_mixing_tasks(self):
        task = self.plan.create('跨周期执行')
        for count in range(1, 4):
            current = self.plan.detail(task['id'])
            self.plan.start(task['id'], current['version'])
            self._await_runs(task['id'], count)

        runs = self.plan.store.list_runs(task['id'])
        with self.plan.db:
            self.plan.db.executemany(
                "UPDATE runs SET state=?, created_at=?, finished_at=?, result=?, error=?, thread_id=? WHERE id=?",
                [
                    ('review', '2026-09-28T09:00:00+00:00', '2026-09-28T09:01:00+00:00', '私有结果一', '', 'private-thread-1', runs[0]['id']),
                    ('failed', '2026-10-01T09:00:00+00:00', '2026-10-01T09:01:00+00:00', '', '私有错误二', 'private-thread-2', runs[1]['id']),
                    ('review', '2026-10-02T09:00:00+00:00', '2026-10-02T09:01:00+00:00', '私有结果三', '', 'private-thread-3', runs[2]['id']),
                ],
            )

        # A normal workbench task can have runs but must never enter planning data.
        ordinary_root = Path(self.temp.name) / 'ordinary'
        ordinary_root.mkdir()
        project = self.plan.store.create_project('非看板项目', str(ordinary_root))
        ordinary = self.plan.store.create_task(project['id'], '非看板任务')
        ordinary = self.plan.store.update_task(ordinary['id'], ordinary['version'], state='ready')
        ordinary_run = self.plan.store.claim(ordinary['id'])
        self.plan.store.finish(ordinary_run['id'], 'review', result='非看板私有结果')

        removed = self.plan.create('已删除看板任务')
        self.plan.start(removed['id'], removed['version'])
        removed = self._await_runs(removed['id'], 1)
        self.plan.remove(removed['id'], removed['version'])

        snapshot = self.plan.snapshot()
        self.assertEqual([task['id']], [item['id'] for item in snapshot['tasks']])
        item = snapshot['tasks'][0]
        self.assertEqual(item['latest_run']['id'], runs[2]['id'])
        self.assertEqual(
            [
                {'id': runs[0]['id'], 'state': 'review', 'created_at': '2026-09-28T09:00:00+00:00', 'finished_at': '2026-09-28T09:01:00+00:00'},
                {'id': runs[1]['id'], 'state': 'failed', 'created_at': '2026-10-01T09:00:00+00:00', 'finished_at': '2026-10-01T09:01:00+00:00'},
                {'id': runs[2]['id'], 'state': 'review', 'created_at': '2026-10-02T09:00:00+00:00', 'finished_at': '2026-10-02T09:01:00+00:00'},
            ],
            item['run_history'],
        )
        self.assertEqual({'id', 'state', 'created_at', 'finished_at'}, set(item['run_history'][0]))


if __name__ == '__main__':
    unittest.main()
