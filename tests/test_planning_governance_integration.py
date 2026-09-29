"""Task-bound governance, explicit egress, and concurrent edit acceptance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.catalog import validate
from codex_workbench.planning import Planning
from codex_workbench.store import StoreError


class Router:
    def __init__(self):
        self.calls = []
        self.during_call = None

    def call(self, capability, payload, **kwargs):
        self.calls.append((capability, payload, kwargs))
        if self.during_call:
            callback, self.during_call = self.during_call, None
            callback()
        return {'text': 'ok'}

    def stats(self, **kwargs):
        return []


class GovernanceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / '.codex').mkdir()
        (self.home / '.codex' / 'AGENTS.md').write_text('local rule only')
        self.home_patch = patch('codex_workbench.planning_rule_manifest.Path.home', return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.router = Router()
        self.plan = Planning(self.home / 'state', model_router=self.router)
        self.addCleanup(self.plan.close)
        self.task = self.plan.create('Public evaluation', 'Return ok', task_card={
            'goal': 'Public test', 'scope': [], 'preserve': [], 'acceptance': [
                {'id': 'A1', 'text': 'Return ok'}], 'facts': [], 'assumptions': [], 'original': 'Public test'})

    def action(self, name, payload):
        detail = self.plan.detail(self.task['id'])
        return self.plan.delivery_action(detail['id'], detail['version'], 'governance_' + name, payload)

    def prepare(self, rule_id='task-card'):
        frozen = self.action('freeze', {'rule_id': rule_id, 'scope': {'scope_id': 'forged'}, 'cases': [
            {'id': 'case-1', 'input': {'prompt': 'Return ok'}, 'expected': {'required': ['ok'], 'forbidden': ['hidden-expectation']}}]})
        self.assertEqual('task:' + self.task['id'], frozen['governance']['suite']['scope']['scope_id'])
        self.assertNotIn('expected', frozen['governance']['suite']['cases'][0])
        feedback = self.action('feedback', {'source_kind': 'user_correction', 'root_cause': 'omitted-answer',
            'independence_key': 'one-observation', 'summary': 'Explicit correction', 'outcome': {'rework': True}})
        candidate = self.action('propose', {'proposal': {'rule_id': rule_id, 'replacement_text': 'Return ok.',
            'rationale': 'Be explicit'}, 'feedback_ids': [feedback['governance']['feedback'][0]['id']], 'user_correction': True})
        return candidate['governance']['candidates'][0]['id']

    def test_preview_and_real_callback_do_not_send_frozen_answers_or_global_rules(self):
        candidate = self.prepare()
        preview = self.action('preview', {'candidate_id': candidate})['governance']['external_review']
        self.assertNotIn('hidden-expectation', json.dumps(preview))
        with self.assertRaises(StoreError) as denied:
            self.action('evaluate', {'candidate_id': candidate})
        self.assertEqual('data_boundary', denied.exception.code)
        self.assertEqual([], self.router.calls)
        result = self.action('evaluate', {'candidate_id': candidate, 'approved_packet_hash': preview['packet_hash']})
        self.assertEqual('passed', result['governance']['evaluations'][0]['summary']['candidate_state'])
        self.assertEqual(2, len(self.router.calls))
        self.assertNotIn('hidden-expectation', json.dumps(self.router.calls))
        self.assertNotIn('local rule only', json.dumps(self.router.calls))
        self.action('evaluate', {'candidate_id': candidate, 'approved_packet_hash': preview['packet_hash']})
        self.assertEqual(2, len(self.router.calls), 'replay must not resend model calls')

    def test_file_rules_are_discovery_only_and_cannot_be_sent(self):
        candidate = self.prepare('global-entry')
        manifest = self.plan.detail(self.task['id'])['governance']['manifests'][0]['manifest']
        global_rule = next(r for r in manifest['rules'] if r['id'] == 'global-entry')
        self.assertEqual('discovered', global_rule['load_state'])
        with self.assertRaises(StoreError) as denied:
            self.action('preview', {'candidate_id': candidate})
        self.assertEqual('data_boundary', denied.exception.code)
        self.assertEqual([], self.router.calls)

    def test_concurrent_task_edit_invalidates_evaluation_without_overwriting_version(self):
        candidate = self.prepare()
        preview = self.action('preview', {'candidate_id': candidate})['governance']['external_review']
        original = self.plan.detail(self.task['id'])['version']
        def concurrent_edit():
            with self.plan.lock, self.plan.db:
                self.plan.db.execute('UPDATE tasks SET version=version+1 WHERE id=?', (self.task['id'],))
        self.router.during_call = concurrent_edit
        result = self.action('evaluate', {'candidate_id': candidate, 'approved_packet_hash': preview['packet_hash']})
        summary = result['governance']['evaluations'][0]['summary']
        self.assertEqual('unknown', summary['candidate_state'])
        self.assertFalse(summary['adoptable'])
        self.assertEqual('rejected', summary['finalize_guard']['state'])
        self.assertEqual(original + 1, result['version'])
        self.assertEqual(1, len(self.router.calls))

    def test_run_capture_distinguishes_injection_from_discovery_and_schema_accepts_actions(self):
        with self.plan.lock:
            manifest, _ = self.plan._capture_rule_manifest(self.task['id'], run_id='run-test')
        rules = {r['id']: r for r in manifest['manifest']['rules']}
        self.assertEqual('injected', rules['task-card']['load_state'])
        self.assertEqual('discovered', rules['global-entry']['load_state'])
        for action in ['governance_capture', 'governance_preview', 'governance_evaluate', 'cancel_handoff', 'reconcile_handoff', 'recover_handoff']:
            validate('planning_delivery', {'task_id': self.task['id'], 'expected_version': self.task['version'], 'action': action, 'payload': {}})

if __name__ == '__main__':
    unittest.main()
