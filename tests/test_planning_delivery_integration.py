"""Planning integration for delivery CAS and run snapshots."""
import base64
import tempfile
import time
import unittest
from pathlib import Path

from codex_workbench.executor import Execution
from codex_workbench.planning import Planning
from codex_workbench.store import StoreError


class _Executor:
    def execute(self, request, cancel, event):
        return Execution('review', 'synthetic review')


class _CaptureExecutor:
    def __init__(self):
        self.prompts = []

    def execute(self, request, cancel, event):
        self.prompts.append(request.prompt)
        return Execution('review', 'synthetic review')


class _Router:
    def call(self, capability, payload, **kwargs):
        assert capability == 'reasoning'
        if kwargs.get('purpose') == 'planning_knowledge_candidates':
            return {'text': '{"knowledge_candidates":[{"title":"可复用检查","content":"运行进入review后仍需人工验收。"}]}' }
        return {'text': '{"decision":"correct","suggestions":["补充人工核对"]}'}

    def stats(self, *, task_id=None, **kwargs):
        return [{'task_id': task_id, 'calls': 1, 'successes': 1, 'average_latency_ms': 10, 'fallbacks': 0}]


class _CloseableContext:
    def __init__(self):
        self.closed = False
        self.cache = self

    def retrieve(self, task, links, assets, **kwargs):
        return {'task_id': task['id'], 'items': []}

    def close(self):
        self.closed = True


class PlanningDeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Planning(Path(self.temp.name), _Executor())
        self.addCleanup(self.plan.close)
        account = self.plan.store.create_execution_account('synthetic', self.temp.name)
        self.plan.store.record_account_subject(account['id'], 'synthetic-subject')
        self.plan.store.set_default_execution_account(account['id'])
        self.card = {
            'goal': '交付可核验结果', 'scope': ['测试'], 'preserve': ['原始需求'],
            'acceptance': [{'id': 'ready', 'text': '人工确认前不得标记已接受'}],
            'facts': [], 'assumptions': [], 'original': '原始文本',
        }

    def _await_run(self, task_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.plan.detail(task_id)
            if detail['runs'] and detail['runs'][0]['state'] != 'running':
                return detail
            time.sleep(.01)
        self.fail('run did not reach a terminal state')

    def test_delivery_actions_are_task_scoped_idempotent_and_snapshot_the_start(self):
        task = self.plan.create('交付任务', '产出一份结果', task_card=self.card)
        asset = self.plan.upload('input.txt', base64.b64encode(b'input').decode(), task_id=task['id'])
        anchored = self.plan.delivery_action(task['id'], task['version'], 'create_anchor', {
            'asset_id': asset['id'], 'descriptor': {'type': 'text_quote', 'quote': 'input', 'start': 0, 'end': 5},
        })
        anchor = anchored['delivery']['anchors'][0]
        annotated = self.plan.delivery_action(task['id'], anchored['version'], 'add_annotation', {
            'anchor_id': anchor['id'], 'asset_id': asset['id'], 'position': {'line': 1},
            'change_text': '补充检查', 'preserve': ['原意'], 'acceptance': ['ready'], 'operation_id': 'one-op',
        })
        with self.assertRaises(StoreError) as error:
            self.plan.delivery_action(task['id'], annotated['version'], 'add_annotation', {
                'anchor_id': anchor['id'], 'asset_id': asset['id'], 'position': {'line': 2},
                'change_text': '不同内容', 'preserve': ['原意'], 'acceptance': ['ready'], 'operation_id': 'one-op',
            })
        self.assertEqual(error.exception.code, 'idempotency_conflict')

        rework = self.plan.delivery_action(task['id'], annotated['version'], 'prepare_rework', {
            'annotation_id': annotated['delivery']['annotations'][0]['id'], 'out_of_scope_candidates': ['后续优化'],
        })
        self.assertEqual('saved', rework['messages'][-1]['state'])
        self.assertIn('范围外候选', rework['messages'][-1]['content'])
        self.assertEqual(1, rework['delivery']['metrics']['rework_count'])
        self.assertEqual('unknown', rework['delivery']['metrics']['first_round_acceptance'])
        self.assertEqual('unknown', rework['delivery']['metrics']['user_correction'])
        after = self.plan.upload('after.txt', base64.b64encode(b'changed').decode(), task_id=task['id'])
        compared = self.plan.delivery_action(task['id'], rework['version'], 'diff_artifacts', {
            'before_asset_id': asset['id'], 'after_asset_id': after['id'],
        })
        self.assertTrue(compared['delivery']['artifact_diff']['changed'])

        started = self.plan.start(task['id'], rework['version'])
        detail = self._await_run(task['id'])
        run = next(value for value in detail['runs'] if value['id'] == started['run_id'])
        self.assertGreaterEqual(run['input_snapshot']['task_version'], annotated['version'])
        self.assertEqual(1, run['input_snapshot']['delivery_revision'])
        self.assertEqual(64, len(run['input_snapshot']['delivery_sha256']))
        self.assertTrue(str(self.plan.output_root / 'runs' / started['run_id']) in self.plan.db.execute(
            'SELECT output_dir FROM planning_runs WHERE run_id=?', (started['run_id'],)).fetchone()['output_dir'])
        self.assertIsNotNone(detail['delivery']['metrics']['runtime_ms'])
        self.assertFalse(detail['delivery']['metrics']['failed'])
        # Terminal runner state is the authoritative source for failure/stop metrics.
        self.plan.db.execute("UPDATE runs SET state='failed' WHERE id=?", (started['run_id'],))
        self.assertTrue(self.plan.detail(task['id'])['delivery']['metrics']['failed'])
        self.plan.db.execute("UPDATE runs SET state='cancelled' WHERE id=?", (started['run_id'],))
        self.assertTrue(self.plan.detail(task['id'])['delivery']['metrics']['stopped'])

    def test_runner_input_has_current_card_and_excludes_stale_knowledge_body(self):
        current = {
            'live': {'knowledge': {'title': '当前知识', 'content': 'CURRENT KNOWLEDGE BODY', 'sha256': 'live-hash'}},
            'stale': {'knowledge': {'title': '旧知识', 'content': 'OLD KNOWLEDGE BODY', 'sha256': 'old-hash'}},
        }
        self.plan.knowledge_reader = lambda scope, key: current[key]
        task = self.plan.create('约束注入', '保留原始任务提示', task_card=self.card)
        linked_stale = self.plan.link_knowledge(task['id'], 'stable', 'stale')
        linked = self.plan.link_knowledge(linked_stale['id'], 'stable', 'live')
        # The stale link remains as provenance, but its current reader lookup fails.
        current.pop('stale')
        capture = _CaptureExecutor()
        self.plan.runner.executor = capture
        started = self.plan.start(linked['id'], linked['version'])
        self._await_run(linked['id'])
        self.assertEqual(1, len(capture.prompts))
        prompt = capture.prompts[0]
        self.assertIn('保留原始任务提示', prompt)
        self.assertIn('交付可核验结果', prompt)
        self.assertIn('原始需求', prompt)
        self.assertIn('人工确认前不得标记已接受', prompt)
        self.assertIn('CURRENT KNOWLEDGE BODY', prompt)
        self.assertNotIn('OLD KNOWLEDGE BODY', prompt)
        self.assertIn('stale', prompt)
        snapshot = self.plan.db.execute('SELECT prompt_sha256 FROM planning_run_snapshots WHERE run_id=?', (started['run_id'],)).fetchone()
        import hashlib
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), snapshot['prompt_sha256'])

    def test_review_uses_current_run_evidence_and_only_returns_checkpoint_suggestions(self):
        self.plan.model_router = _Router()
        task = self.plan.create('评审任务', '交付', task_card=self.card)
        started = self.plan.start(task['id'], task['version'])
        completed = self._await_run(task['id'])
        evidenced = self.plan.delivery_action(task['id'], completed['version'], 'record_evidence', {
            'criterion_id': 'ready', 'executor_state': 'completed', 'validation_state': 'passed',
            'source_ref': {'kind': 'run', 'id': started['run_id']}, 'summary': '运行进入评审',
            'check_name': '运行状态', 'result': 'review',
        })
        reviewed = self.plan.delivery_action(task['id'], evidenced['version'], 'review_delivery', {})
        self.assertEqual('correct', reviewed['delivery']['review']['decision'])
        self.assertFalse(reviewed['delivery']['review']['starts_runner'])
        self.assertEqual(1, len(reviewed['delivery']['checkpoints']))
        self.assertEqual('pending', reviewed['delivery']['metrics']['first_round_acceptance'])
        accepted = self.plan.delivery_action(task['id'], reviewed['version'], 'accept_evidence', {
            'evidence_id': reviewed['delivery']['evidence'][0]['id'], 'accepted': True,
        })
        self.assertEqual('accepted', accepted['delivery']['metrics']['first_round_acceptance'])

    def test_accepted_evidence_can_generate_task_scoped_router_candidates(self):
        self.plan.model_router = _Router()
        self.plan.knowledge_reader = lambda scope, key: {'knowledge': {'title': '已选知识', 'content': '受审核内容', 'sensitivity': 'internal'}}
        task = self.plan.create('候选任务', '交付', task_card=self.card)
        started = self.plan.start(task['id'], task['version'])
        completed = self._await_run(task['id'])
        evidenced = self.plan.delivery_action(task['id'], completed['version'], 'record_evidence', {
            'criterion_id': 'ready', 'executor_state': 'completed', 'validation_state': 'passed',
            'source_ref': {'kind': 'run', 'id': started['run_id']}, 'summary': '审查完成',
            'check_name': '运行状态', 'result': 'review', 'manual_validation': True,
        })
        accepted = self.plan.delivery_action(task['id'], evidenced['version'], 'accept_evidence', {
            'evidence_id': evidenced['delivery']['evidence'][0]['id'], 'accepted': True,
        })
        linked = self.plan.link_knowledge(task['id'], 'scope-a', 'key-a')
        candidates = self.plan.knowledge_candidates(linked['id'], {}, 'scope-a', {'kind': 'run', 'id': started['run_id']})
        self.assertEqual(['可复用检查'], [item['title'] for item in candidates])
        self.assertEqual('candidate', candidates[0]['status'])
        self.assertTrue(candidates[0]['local_only'])
        self.assertEqual({'status': 'unknown', 'reason': 'pending_confirmation'}, candidates[0]['applicability'])
        self.assertEqual(started['run_id'], candidates[0]['source']['id'])
        self.assertEqual(accepted['delivery']['evidence'][0]['card_revision'], candidates[0]['card_revision'])
        self.assertEqual(accepted['delivery']['evidence'][0]['created_at'], candidates[0]['verified_at'])
        self.assertEqual(candidates, self.plan.detail(linked['id'])['delivery']['knowledge_candidates'])
        context = self.plan.context(linked['id'])
        self.assertEqual(linked['id'], context['model_routing'][0]['task_id'])

    def test_supplied_candidates_require_current_accepted_evidence(self):
        self.plan.knowledge_reader = lambda scope, key: {'knowledge': {'title': '已选知识', 'content': '受审核内容'}}
        task = self.plan.create('手工候选', '交付', task_card=self.card)
        started = self.plan.start(task['id'], task['version'])
        completed = self._await_run(task['id'])
        evidenced = self.plan.delivery_action(task['id'], completed['version'], 'record_evidence', {
            'criterion_id': 'ready', 'executor_state': 'completed', 'validation_state': 'passed',
            'source_ref': {'kind': 'run', 'id': started['run_id']}, 'summary': '审查完成',
            'check_name': '运行状态', 'result': 'review', 'manual_validation': True,
        })
        linked = self.plan.link_knowledge(task['id'], 'scope-a', 'key-a')
        supplied = {'knowledge_candidates': [{'title': '手工候选', 'content': '只有人工验收后才可待审。'}]}
        with self.assertRaises(StoreError) as error:
            self.plan.knowledge_candidates(linked['id'], supplied, 'scope-a', {'kind': 'run', 'id': started['run_id']})
        self.assertEqual('validation', error.exception.code)
        accepted = self.plan.delivery_action(task['id'], linked['version'], 'accept_evidence', {
            'evidence_id': evidenced['delivery']['evidence'][0]['id'], 'accepted': True,
        })
        candidates = self.plan.knowledge_candidates(accepted['id'], supplied, 'scope-a', {'kind': 'run', 'id': started['run_id']})
        self.assertEqual(['手工候选'], [row['title'] for row in candidates])
        self.assertFalse(candidates[0]['reviewed'])

    def test_close_releases_context_cache_when_engine_supports_close(self):
        engine = _CloseableContext()
        self.plan.context_engine = engine
        self.plan.close()
        self.assertTrue(engine.closed)


if __name__ == '__main__':
    unittest.main()
