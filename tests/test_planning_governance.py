import hashlib
import sqlite3
import unittest

from codex_workbench.planning_governance import Governance
from codex_workbench.store import StoreError


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.governance = Governance(self.db)
        self.task = "task-a"
        self.scope = {"project_id": "project-a", "scope_id": "scope-a", "kind": "project"}

    def manifest(self):
        return self.governance.record_manifest(self.task, "run-a", {"rules": [
            {"id": "project:rules", "scope": "project", "source": "authorised-collector", "content": "保留权限约束",
             "load_state": "loaded", "evidence_ref": "runtime:rules", "byte_count": len("保留权限约束".encode())}
        ]})

    def suite(self):
        manifest = self.manifest()
        text = "保留权限约束"
        return self.governance.freeze_suite(self.task, {"scope": self.scope, "selection": "held-out-before-mining",
            "baseline_manifest_hash": manifest["manifest_hash"],
            "baseline": {"rule_id": "project:rules", "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()}, "cases": [
            {"id": "positive", "input": {"prompt": "hello"}, "expected": {"required": ["ok"], "forbidden": ["bad"]}}
        ]})

    def feedback(self, source, independent, *, correction=False):
        return self.governance.feedback(self.task, {"source_kind": "user_correction" if correction else source,
            "root_cause": "same-root", "independence_key": independent, "summary": "evidence", "outcome": {"rework": True}})

    def propose(self, feedback_ids, **extra):
        manifest = self.manifest()
        return self.governance.propose(self.task, {"scope": self.scope, "baseline_manifest_hash": manifest["manifest_hash"],
            "proposal": {"rule_id": "project:rules", "replacement_text": "明确保留权限约束", "rationale": "减少遗漏"}, "feedback_ids": feedback_ids,
            "budget": {"constraints": [{"id": "permission-1", "kind": "permission", "content": "do not deploy"}]}, **extra})

    def test_suite_is_immutable_and_required_before_candidate(self):
        self.manifest()
        with self.assertRaises(StoreError) as raised:
            self.governance.propose(self.task, {"scope": self.scope, "baseline_manifest_hash": "x", "proposal": {}, "feedback_ids": []})
        self.assertEqual("conflict", raised.exception.code)
        self.suite()
        with self.assertRaises(StoreError) as raised:
            self.governance.freeze_suite(self.task, {"scope": self.scope, "cases": [{"id": "different", "input": {}, "expected": {"required": ["x"]}}]})
        self.assertEqual("conflict", raised.exception.code)

    def test_manifest_preserves_load_evidence_and_rejects_duplicate_ids(self):
        result = self.manifest()
        rule = result["manifest"]["rules"][0]
        self.assertEqual(hashlib.sha256("保留权限约束".encode()).hexdigest(), rule["content_hash"])
        self.assertEqual("loaded", rule["load_state"])
        self.assertEqual("runtime:rules", rule["evidence_ref"])
        with self.assertRaises(StoreError):
            self.governance.record_manifest(self.task, "run-b", {"rules": [
                {"id": "duplicate", "scope": "project", "content": "one"},
                {"id": "duplicate", "scope": "project", "content": "two"},
            ]})

    def test_common_root_needs_independent_sources_but_user_correction_can_be_singleton(self):
        self.suite()
        first = self.feedback("run", "shared-run")
        second = self.feedback("review", "shared-run")
        with self.assertRaises(StoreError) as raised:
            self.propose([first["id"], second["id"]])
        self.assertEqual("validation", raised.exception.code)
        correction = self.feedback("run", "user-message-9", correction=True)
        candidate = self.propose([correction["id"]], user_correction=True)
        self.assertEqual("proposed", candidate["state"])

    def test_scope_and_protected_budget_constraints_are_enforced(self):
        self.suite()
        evidence = self.feedback("run", "run-1")
        self.feedback("review", "review-1")
        manifest = self.manifest()
        with self.assertRaises(StoreError):
            self.governance.propose(self.task, {"scope": {**self.scope, "scope_id": "other"}, "baseline_manifest_hash": manifest["manifest_hash"], "proposal": {}, "feedback_ids": [evidence["id"]]})
        with self.assertRaises(StoreError):
            self.governance.propose(self.task, {"scope": self.scope, "baseline_manifest_hash": manifest["manifest_hash"], "proposal": {}, "feedback_ids": [evidence["id"]],
                "budget": {"constraints": [{"id": "permission-1", "kind": "permission", "content": "no deploy"}], "remove_ids": ["permission-1"]}})

    def test_real_callback_is_required_and_evaluation_is_idempotent(self):
        self.suite()
        evidence_a = self.feedback("run", "run-1")
        evidence_b = self.feedback("review", "review-1")
        candidate = self.propose([evidence_a["id"], evidence_b["id"]])
        calls = []
        evaluation = self.governance.evaluate(self.task, candidate["id"], lambda _case, variant: calls.append(variant) or {"actual": True, "text": "ok"})
        self.assertEqual("passed", evaluation["summary"]["candidate_state"])
        self.assertEqual("保留权限约束", calls[0]["value"]["text"])
        replay = self.governance.evaluate(self.task, candidate["id"], lambda *_: self.fail("must not repeat evaluator"))
        self.assertEqual(evaluation["id"], replay["id"])
        self.assertTrue(evaluation["results"][0]["comparison"]["known"])

    def test_baseline_timeout_is_unknown_and_blocks_adoption(self):
        self.suite()
        candidate = self.propose([self.feedback("run", "run-1")["id"], self.feedback("review", "review-1")["id"]])

        def evaluator(_case, variant):
            if variant["name"] == "baseline":
                raise TimeoutError()
            return {"actual": True, "text": "ok"}

        result = self.governance.evaluate(self.task, candidate["id"], evaluator)
        self.assertEqual("unknown", result["summary"]["candidate_state"])
        self.assertFalse(result["summary"]["adoptable"])
        self.assertEqual("unknown", result["results"][0]["state"])
        self.assertEqual("TimeoutError", result["results"][0]["error_type"])
        self.assertFalse(result["results"][1]["comparison"]["improvement"])

    def test_finalize_guard_rejection_records_unknown_non_adoptable_evaluation(self):
        self.suite()
        candidate = self.propose([self.feedback("run", "run-1")["id"], self.feedback("review", "review-1")["id"]])
        observed = []
        result = self.governance.evaluate(
            self.task, candidate["id"], lambda _case, _variant: {"actual": True, "text": "ok"},
            finalize_guard=lambda context: observed.append(context) or False,
        )
        self.assertEqual("unknown", result["summary"]["candidate_state"])
        self.assertFalse(result["summary"]["adoptable"])
        self.assertEqual("rejected", result["summary"]["finalize_guard"]["state"])
        self.assertEqual(candidate["id"], observed[0]["candidate_id"])
        self.assertEqual("unknown", self.governance.detail(self.task)["candidates"][0]["state"])

    def test_evaluation_preview_never_leaks_expected_conditions(self):
        self.suite()
        candidate = self.propose([self.feedback("run", "run-1")["id"], self.feedback("review", "review-1")["id"]])
        preview = self.governance.preview_evaluation(self.task, candidate["id"])
        self.assertEqual("project:rules", preview["baseline"]["rule_id"])
        self.assertEqual("明确保留权限约束", preview["candidate"]["replacement_text"])
        self.assertEqual({"id", "input"}, set(preview["cases"][0]))
        self.assertNotIn("expected", preview["cases"][0])

    def test_detail_groups_causes_and_exposes_result_metrics(self):
        self.feedback("run", "same")
        self.feedback("review", "same")
        detail = self.governance.detail(self.task)
        self.assertEqual(2, detail["causes"][0]["observations"])
        self.assertEqual(1, detail["causes"][0]["reported_independence_count"])
        self.assertFalse(detail["causes"][0]["independence_verified"])
        self.assertEqual(2, detail["metrics"]["rework"]["true"])


if __name__ == "__main__":
    unittest.main()
