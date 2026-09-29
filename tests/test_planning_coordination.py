"""Regression coverage for durable review handoffs and declared work boundaries."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from codex_workbench.executor import Execution
from codex_workbench.planning import Planning
from codex_workbench.planning_coordination import PlanningCoordination
from codex_workbench.store import StoreError


class PlanningCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.coordination = PlanningCoordination(self.db)
        self.db.executescript("""
            CREATE TABLE projects(id TEXT PRIMARY KEY, cwd TEXT NOT NULL);
            CREATE TABLE tasks(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE planning_tasks(task_id TEXT PRIMARY KEY, deleted_at TEXT);
        """)
        self.db.execute("INSERT INTO projects VALUES('project-1', ?)", (tempfile.gettempdir(),))
        for task_id, state in (("task-1", "review"), ("task-2", "review"), ("task-3", "running")):
            self.db.execute("INSERT INTO tasks VALUES(?,?,?)", (task_id, "project-1", state))
            self.db.execute("INSERT INTO planning_tasks VALUES(?,NULL)", (task_id,))

    def handoff(self, fingerprint="v1"):
        return self.coordination.prepare("task-1", fingerprint, {"task": "task-1", "evidence": ["ok"]})

    def test_handoff_deduplicates_across_persisted_connection(self):
        with tempfile.NamedTemporaryFile() as database:
            first_db = sqlite3.connect(database.name)
            first_db.row_factory = sqlite3.Row
            first = PlanningCoordination(first_db).prepare("task-1", "v1", {"evidence": ["ok"]})
            first_db.commit()
            first_db.close()

            second_db = sqlite3.connect(database.name)
            second_db.row_factory = sqlite3.Row
            second = PlanningCoordination(second_db).prepare("task-1", "v1", {"evidence": ["ok"]})
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(1, second_db.execute("SELECT count(*) FROM planning_handoffs").fetchone()[0])
            second_db.close()

    def test_stale_or_unknown_handoff_cannot_be_accepted_or_resubmitted(self):
        packet = self.handoff()
        self.coordination.begin("task-1", packet["id"], "v1", 101)
        self.coordination.finish("task-1", packet["id"], response={"decision": "correct", "suggestions": ["补充验收"]})
        with self.assertRaises(StoreError) as stale:
            self.coordination.decide("task-1", packet["id"], "v2", 0, "accepted", "材料已经变化")
        self.assertEqual("version_conflict", stale.exception.code)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM planning_advice_decisions").fetchone()[0])

        retry = self.coordination.prepare("task-2", "v1", {"task": "task-2"})
        self.coordination.begin("task-2", retry["id"], "v1", 102)
        self.coordination.finish("task-2", retry["id"], error="timeout")
        replay = self.coordination.prepare("task-2", "v1", {"task": "task-2"})
        self.assertEqual("unknown", replay["state"])
        with self.assertRaises(StoreError) as rejected:
            self.coordination.begin("task-2", replay["id"], "v1", 103)
        self.assertEqual("conflict", rejected.exception.code)

    def test_recover_preserves_a_sending_handoff_with_live_owner(self):
        packet = self.handoff()
        self.coordination.begin("task-1", packet["id"], "v1", 456)
        self.coordination.recover(lambda owner_pid: owner_pid == 456)
        stored = self.db.execute("SELECT state,owner_pid FROM planning_handoffs WHERE id=?", (packet["id"],)).fetchone()
        self.assertEqual(("sending", 456), (stored["state"], stored["owner_pid"]))

    def test_unknown_handoff_requires_evidence_based_reconciliation_and_cannot_resend(self):
        packet = self.handoff()
        self.coordination.begin("task-1", packet["id"], "v1", 456)
        self.coordination.finish("task-1", packet["id"], error="timeout")
        reconciled = self.coordination.reconcile(
            "task-1", packet["id"], "v1", provider_operation_id="OP42", status_query_ref="provider_op_42",
            side_effect_state="submitted", last_event_id="event-7", source={"kind": "provider_status", "id": "OP42"},
            status_evidence="provider reports operation is still running",
        )
        self.assertEqual("unknown", reconciled["state"])
        self.assertIsNone(reconciled["response"])
        self.assertEqual("OP42", reconciled["provider_operation_id"])
        self.assertEqual({"id": "OP42", "kind": "provider_status"}, reconciled["reconciliation_source"])
        with self.assertRaises(StoreError) as resend:
            self.coordination.begin("task-1", packet["id"], "v1", 457)
        self.assertEqual("conflict", resend.exception.code)
        with self.assertRaises(StoreError) as mismatch:
            self.coordination.reconcile(
                "task-1", packet["id"], "v1", provider_operation_id="OP43", status_query_ref="provider_op_42",
                side_effect_state="submitted", source={"kind": "provider_status", "id": "OP43"},
                status_evidence="untrusted replacement operation",
            )
        self.assertEqual("idempotency_conflict", mismatch.exception.code)

    def test_prepared_handoff_can_be_cancelled_without_treating_disconnect_as_cancel(self):
        packet = self.handoff()
        cancelled = self.coordination.cancel_prepared("task-1", packet["id"], "v1", "用户取消")
        self.assertEqual("cancelled", cancelled["state"])
        with self.assertRaises(StoreError) as blocked:
            self.coordination.begin("task-1", packet["id"], "v1", 999)
        self.assertEqual("conflict", blocked.exception.code)

    def test_reconciled_completion_rejects_operation_mismatch(self):
        packet = self.handoff()
        self.coordination.begin("task-1", packet["id"], "v1", 456)
        self.coordination.finish("task-1", packet["id"], error="timeout")
        self.coordination.reconcile(
            "task-1", packet["id"], "v1", provider_operation_id="OP42", status_query_ref="provider_op_42",
            side_effect_state="completed", source={"kind": "provider_status", "id": "OP42"},
            status_evidence="provider reports the operation completed",
        )
        with self.assertRaises(StoreError) as mismatch:
            self.coordination.reconcile_response(
                "task-1", packet["id"], "v1", provider_operation_id="OP43",
                response={"decision": "continue", "suggestions": []},
                source={"kind": "provider_result", "id": "OP43"}, status_evidence="wrong operation",
            )
        self.assertEqual("idempotency_conflict", mismatch.exception.code)

    def test_reconciled_completion_with_structured_response_can_be_decided(self):
        packet = self.handoff()
        self.coordination.begin("task-1", packet["id"], "v1", 456)
        self.coordination.finish("task-1", packet["id"], error="timeout")
        self.coordination.reconcile(
            "task-1", packet["id"], "v1", provider_operation_id="OP42", status_query_ref="provider_op_42",
            side_effect_state="completed", source={"kind": "provider_status", "id": "OP42"},
            status_evidence="provider reports the operation completed",
        )
        restored = self.coordination.reconcile_response(
            "task-1", packet["id"], "v1", provider_operation_id="OP42",
            response={"decision": "correct", "suggestions": ["补充来源"]},
            source={"kind": "provider_result", "id": "OP42"}, status_evidence="signed provider result retrieved",
        )
        self.assertEqual("received", restored["state"])
        self.assertEqual("correct", restored["response"]["decision"])
        self.coordination.decide("task-1", packet["id"], "v1", 0, "accepted", "已核对")

    def test_plan_blocks_cycles_unaccepted_dependencies_and_shared_execution(self):
        verify = lambda task_id: self.db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone() or (_ for _ in ()).throw(StoreError("not_found", "missing"))
        self.coordination.save_plan("task-2", {"dependencies": ["task-1"]}, verify)
        with self.assertRaises(StoreError) as cycle:
            self.coordination.save_plan("task-1", {"dependencies": ["task-2"]}, verify)
        self.assertEqual("validation", cycle.exception.code)

        self.coordination.save_plan("task-2", {}, verify)
        self.coordination.save_plan("task-1", {"dependencies": ["task-2"], "paths": ["src/shared.py"], "resources": ["database"]}, verify)
        self.coordination.save_plan("task-3", {"paths": ["src"], "resources": ["database"]}, verify)
        readiness = self.coordination.readiness("task-1", "project-1", lambda task_id: False)
        self.assertFalse(readiness["ready"])
        self.assertTrue(any(item["kind"] == "dependency" and item["task_id"] == "task-2" for item in readiness["blockers"]))
        ownership = next(item for item in readiness["blockers"] if item["kind"] == "ownership")
        self.assertEqual("task-3", ownership["task_id"])
        self.assertEqual(["database"], ownership["resources"])


class _Executor:
    def execute(self, request, cancel, event):
        return Execution("review", "synthetic review")


class _ReviewRouter:
    def __init__(self, failure=False):
        self.failure = failure
        self.calls = 0

    def call(self, capability, payload, **kwargs):
        self.calls += 1
        self.assert_capability(capability, kwargs)
        if self.failure:
            raise TimeoutError("synthetic timeout")
        return {"text": '{"decision":"correct","suggestions":["补充人工核对"]}'}

    @staticmethod
    def assert_capability(capability, kwargs):
        if capability != "reasoning" or kwargs.get("allow_fallback") is not False:
            raise AssertionError("review must issue one non-fallback model request")


class PlanningReviewReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Planning(Path(self.temp.name), _Executor())
        self.addCleanup(self.plan.close)
        account = self.plan.store.create_execution_account("synthetic", self.temp.name)
        self.plan.store.record_account_subject(account["id"], "synthetic-subject")
        self.plan.store.set_default_execution_account(account["id"])
        self.card = {
            "goal": "交付可核验结果", "scope": ["测试"], "preserve": ["原始需求"],
            "acceptance": [{"id": "ready", "text": "人工确认前不得标记已接受"}],
            "facts": [], "assumptions": [], "original": "原始文本",
        }

    def _evidenced_task(self):
        task = self.plan.create("评审任务", "交付", task_card=self.card)
        return self._record_current_run_evidence(task)

    def _record_current_run_evidence(self, task):
        started = self.plan.start(task["id"], task["version"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = self.plan.detail(task["id"])
            run = next((item for item in detail["runs"] if item["id"] == started["run_id"]), None)
            if run is not None and run["state"] != "running":
                return self.plan.delivery_action(task["id"], detail["version"], "record_evidence", {
                    "criterion_id": "ready", "executor_state": "completed", "validation_state": "passed",
                    "source_ref": {"kind": "run", "id": started["run_id"]}, "summary": "运行进入评审",
                    "check_name": "运行状态", "result": "review",
                })
            time.sleep(.01)
        self.fail("run did not reach a terminal state")

    def test_review_replay_uses_saved_response_without_a_second_model_call(self):
        router = _ReviewRouter()
        self.plan.model_router = router
        evidenced = self._evidenced_task()
        first = self.plan.delivery_action(evidenced["id"], evidenced["version"], "review_delivery", {})
        replay = self.plan.delivery_action(first["id"], first["version"], "review_delivery", {})
        self.assertEqual(1, router.calls)
        self.assertEqual(first["delivery"]["review"]["packet_id"], replay["delivery"]["review"]["packet_id"])

    def test_failed_review_is_recorded_unknown_and_not_automatically_resent(self):
        router = _ReviewRouter(failure=True)
        self.plan.model_router = router
        evidenced = self._evidenced_task()
        with self.assertRaises(StoreError) as first:
            self.plan.delivery_action(evidenced["id"], evidenced["version"], "review_delivery", {})
        self.assertEqual("unavailable", first.exception.code)
        with self.assertRaises(StoreError) as replay:
            self.plan.delivery_action(evidenced["id"], evidenced["version"], "review_delivery", {})
        self.assertEqual("conflict", replay.exception.code)
        self.assertEqual(1, router.calls)

    def test_used_evidence_blocks_follow_up_review_before_model_call(self):
        router = _ReviewRouter()
        self.plan.model_router = router
        first_evidence = self._evidenced_task()
        first_review = self.plan.delivery_action(first_evidence["id"], first_evidence["version"], "review_delivery", {})
        second_evidence = self._record_current_run_evidence(first_review)

        with self.assertRaises(StoreError) as rejected:
            self.plan.delivery_action(second_evidence["id"], second_evidence["version"], "review_delivery", {})
        self.assertEqual("evidence_loop", rejected.exception.code)
        self.assertEqual(1, router.calls)


if __name__ == "__main__":
    unittest.main()
