import sqlite3
import threading
import unittest

from codex_workbench.planning_delivery import PlanningDelivery
from codex_workbench.store import StoreError


class PlanningDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.delivery = PlanningDelivery(self.db, threading.RLock())
        self.db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY,sha256 TEXT)")
        self.db.execute("CREATE TABLE asset_refs(asset_id TEXT,task_id TEXT)")
        self.db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,task_id TEXT,state TEXT)")
        self.db.execute("CREATE TABLE planning_run_snapshots(run_id TEXT PRIMARY KEY,content_fingerprint TEXT)")
        self.task = {"id": "task-1", "version": 1}
        self.asset = {"id": "asset-1", "sha256": "a" * 64}
        self.run = {"id": "run-1", "task_id": "task-1"}
        self.message = {"id": "message-1", "task_id": "task-1"}
        self.db.execute("INSERT INTO assets VALUES(?,?)", (self.asset["id"], self.asset["sha256"]))
        self.db.execute("INSERT INTO asset_refs VALUES(?,?)", (self.asset["id"], self.task["id"]))
        self.db.execute("INSERT INTO runs VALUES(?,?,?)", (self.run["id"], self.task["id"], "review"))
        self.delivery.save_card("task-1", self.task["version"], self.card(), verify_task=self.verify, advance_task=self.advance)
        self.db.execute("INSERT INTO planning_run_snapshots VALUES(?,?)",
                        (self.run["id"], self.delivery.content_fingerprint(self.task["id"])))

    def verify(self, task_id, version):
        if task_id != self.task["id"] or version != self.task["version"]:
            raise StoreError("version_conflict", "stale")
        return self.task.copy()

    def advance(self, task):
        self.assertEqual(self.task["id"], task["id"])
        self.task["version"] += 1
        return self.task.copy()

    def card(self):
        return {"goal": "交付领域模型", "scope": ["后端"], "preserve": ["既有任务"],
                "acceptance": [{"id": "c1", "text": "验证通过"}], "facts": ["SQLite"],
                "assumptions": ["上层已授权"], "original": "实现可审计交付"}

    def anchor(self):
        return self.delivery.create_anchor("task-1", self.task["version"], self.asset, self.run, self.message,
                                           {"type": "text_quote", "quote": "原文", "start": 0, "end": 2},
                                           verify_task=self.verify, advance_task=self.advance)

    def test_stale_anchor_refuses_annotation_after_asset_hash_changes(self):
        anchor = self.anchor()
        changed_asset = {"id": "asset-1", "sha256": "b" * 64}
        with self.assertRaises(StoreError) as raised:
            self.delivery.add_annotation("task-1", self.task["version"], anchor["id"], changed_asset, self.run, self.message,
                                         position={"line": 1}, change_text="改这里", preserve=["保留原意"], acceptance=["c1"], operation_id="op-stale",
                                         verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("stale_anchor", raised.exception.code)

    def test_annotation_operation_is_idempotent_but_reuse_with_other_payload_is_rejected(self):
        anchor = self.anchor()
        kwargs = dict(position={"line": 1}, change_text="改这里", preserve=["保留原意"], acceptance=["c1"], operation_id="op-1",
                      verify_task=self.verify, advance_task=self.advance)
        first = self.delivery.add_annotation("task-1", self.task["version"], anchor["id"], self.asset, self.run, self.message, **kwargs)
        replay = self.delivery.add_annotation("task-1", 1, anchor["id"], self.asset, self.run, self.message, **kwargs)
        self.assertFalse(first["idempotent"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(StoreError) as raised:
            self.delivery.add_annotation("task-1", self.task["version"], anchor["id"], self.asset, self.run, self.message,
                                         position={"line": 2}, change_text="换内容", preserve=["保留原意"], acceptance=["c1"], operation_id="op-1",
                                         verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("idempotency_conflict", raised.exception.code)

    def test_acceptance_requires_validation_evidence_and_does_not_auto_accept(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="unknown", source_ref={"kind":"asset", **self.asset}, summary="输出存在", check_name="文件检查", result="未验证", verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("pending", evidence["user_acceptance"])
        with self.assertRaises(StoreError) as raised:
            self.delivery.accept_evidence("task-1", self.task["version"], evidence["id"], accepted=True,
                                          verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("unverified_acceptance", raised.exception.code)

    def test_checkpoint_is_evidence_bounded_and_limits_correction_rounds(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind":"run", "id":self.run["id"]}, summary="运行完成", check_name="运行检查", result="通过", verify_task=self.verify, advance_task=self.advance)
        first = self.delivery.checkpoint("task-1", self.task["version"], [evidence["id"]], decision="correct", suggestions=["修订一处"],
                                         verify_task=self.verify, advance_task=self.advance)
        self.assertFalse(first["starts_runner"])
        with self.assertRaises(StoreError) as raised:
            self.delivery.checkpoint("task-1", self.task["version"], [evidence["id"]], decision="correct", suggestions=["重复"],
                                     verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("evidence_loop", raised.exception.code)
        second_evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                         validation_state="failed", source_ref={"kind":"asset", **self.asset}, summary="检查失败", check_name="文件检查", result="失败", verify_task=self.verify, advance_task=self.advance)
        self.delivery.checkpoint("task-1", self.task["version"], [second_evidence["id"]], decision="correct", suggestions=["再修订"],
                                 verify_task=self.verify, advance_task=self.advance)
        third_evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                        validation_state="passed", source_ref={"kind":"asset", **self.asset}, summary="检查通过", check_name="文件检查", result="通过", verify_task=self.verify, advance_task=self.advance)
        with self.assertRaises(StoreError) as raised:
            self.delivery.checkpoint("task-1", self.task["version"], [third_evidence["id"]], decision="stop", suggestions=[],
                                     verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("correction_limit", raised.exception.code)

    def test_evidence_becomes_stale_when_current_execution_inputs_change(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                                  summary="运行完成", check_name="运行检查", result="通过",
                                                  verify_task=self.verify, advance_task=self.advance)
        self.db.execute("INSERT INTO assets VALUES(?,?)", ("asset-2", "b" * 64))
        self.db.execute("INSERT INTO asset_refs VALUES(?,?)", ("asset-2", self.task["id"]))
        freshness = self.delivery.evidence_freshness("task-1", evidence["id"])
        self.assertEqual({"valid": False, "reason": "attachments_changed"},
                         {key: freshness[key] for key in ("valid", "reason")})
        with self.assertRaises(StoreError) as raised:
            self.delivery.accept_evidence("task-1", self.task["version"], evidence["id"], accepted=True,
                                          verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("stale_evidence", raised.exception.code)

    def test_run_evidence_requires_recorded_execution_input_fingerprint(self):
        self.db.execute("DELETE FROM planning_run_snapshots WHERE run_id=?", (self.run["id"],))
        with self.assertRaises(StoreError) as raised:
            self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                          validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                          summary="运行完成", check_name="运行检查", result="通过",
                                          verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("source_unverifiable", raised.exception.code)

    def test_evidence_becomes_stale_when_task_card_changes(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                                  summary="运行完成", check_name="运行检查", result="通过",
                                                  verify_task=self.verify, advance_task=self.advance)
        changed = self.card()
        changed["acceptance"] = [{"id": "c1", "text": "验证通过并保留记录"}]
        self.delivery.save_card("task-1", self.task["version"], changed,
                                verify_task=self.verify, advance_task=self.advance)
        freshness = self.delivery.evidence_freshness("task-1", evidence["id"])
        self.assertEqual({"valid": False, "reason": "card_changed"},
                         {key: freshness[key] for key in ("valid", "reason")})

    def test_evidence_does_not_expire_from_its_own_task_version_advance(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                                  summary="运行完成", check_name="运行检查", result="通过",
                                                  verify_task=self.verify, advance_task=self.advance)
        freshness = self.delivery.evidence_freshness("task-1", evidence["id"])
        self.assertTrue(freshness["valid"])
        accepted = self.delivery.accept_evidence("task-1", self.task["version"], evidence["id"], accepted=True,
                                                 verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("accepted", accepted["user_acceptance"])

    def test_evidence_becomes_stale_when_a_new_current_run_exists(self):
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                                  summary="运行完成", check_name="运行检查", result="通过",
                                                  verify_task=self.verify, advance_task=self.advance)
        self.db.execute("INSERT INTO runs VALUES(?,?,?)", ("run-2", self.task["id"], "review"))
        freshness = self.delivery.evidence_freshness("task-1", evidence["id"])
        self.assertEqual({"valid": False, "reason": "current_run_changed"},
                         {key: freshness[key] for key in ("valid", "reason")})
        with self.assertRaises(StoreError) as raised:
            self.delivery.checkpoint("task-1", self.task["version"], [evidence["id"]], decision="continue", suggestions=[],
                                     verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("stale_evidence", raised.exception.code)

    def test_evidence_becomes_stale_when_ownership_plan_changes(self):
        self.db.execute("CREATE TABLE planning_work_ownership(task_id TEXT PRIMARY KEY,plan TEXT NOT NULL,updated_at TEXT NOT NULL)")
        self.db.execute("INSERT INTO planning_work_ownership VALUES(?,?,?)",
                        ("task-1", '{"dependencies":[],"owner":"agent-a","paths":["src"]}', "2026-09-29T00:00:00Z"))
        self.db.execute("UPDATE planning_run_snapshots SET content_fingerprint=? WHERE run_id=?",
                        (self.delivery.content_fingerprint("task-1"), self.run["id"]))
        evidence = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="c1", executor_state="completed",
                                                  validation_state="passed", source_ref={"kind": "run", "id": self.run["id"]},
                                                  summary="运行完成", check_name="运行检查", result="通过",
                                                  verify_task=self.verify, advance_task=self.advance)
        self.db.execute("UPDATE planning_work_ownership SET plan=? WHERE task_id=?",
                        ('{"dependencies":[],"owner":"agent-b","paths":["src"]}', "task-1"))
        freshness = self.delivery.evidence_freshness("task-1", evidence["id"])
        self.assertEqual({"valid": False, "reason": "execution_input_changed"},
                         {key: freshness[key] for key in ("valid", "reason")})

    def test_stable_acceptance_ids_keep_unrelated_evidence_current_and_retain_removed_history(self):
        card = self.card()
        card["acceptance"] = [
            {"id": "a1", "text": "接口返回正确"},
            {"id": "a2", "text": "失败状态可见"},
            {"id": "a3", "text": "原有权限保持"},
        ]
        self.delivery.save_card("task-1", self.task["version"], card,
                                verify_task=self.verify, advance_task=self.advance)
        source = {"kind": "asset", **self.asset}
        a1 = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="a1",
                                           executor_state="completed", validation_state="passed", source_ref=source,
                                           summary="接口验证", check_name="接口", result="通过",
                                           verify_task=self.verify, advance_task=self.advance)
        a2 = self.delivery.record_evidence("task-1", self.task["version"], criterion_id="a2",
                                           executor_state="completed", validation_state="passed", source_ref=source,
                                           summary="失败验证", check_name="失败", result="通过",
                                           verify_task=self.verify, advance_task=self.advance)
        changed = self.card()
        changed["acceptance"] = [
            {"id": "a1", "text": "接口返回正确"},
            {"id": "a2", "text": "失败状态可见且可追踪"},
            {"id": "a3", "text": "原有权限保持"},
        ]
        self.delivery.save_card("task-1", self.task["version"], changed,
                                verify_task=self.verify, advance_task=self.advance)
        self.assertTrue(self.delivery.evidence_freshness("task-1", a1["id"])["valid"])
        self.assertEqual("card_changed", self.delivery.evidence_freshness("task-1", a2["id"])["reason"])

        removed = self.card()
        removed["acceptance"] = [
            {"id": "a1", "text": "接口返回正确"},
            {"id": "a3", "text": "原有权限保持"},
        ]
        self.delivery.save_card("task-1", self.task["version"], removed,
                                verify_task=self.verify, advance_task=self.advance)
        statuses = {item["id"]: item for item in self.delivery.acceptance_condition_statuses("task-1")}
        self.assertTrue(statuses["a1"]["current"])
        self.assertTrue(statuses["a1"]["evidence"][0]["freshness"]["valid"])
        self.assertFalse(statuses["a2"]["current"])
        self.assertEqual("criterion_removed", statuses["a2"]["reason"])

    def test_card_uses_parent_version_contract(self):
        saved = self.delivery.save_card("task-1", self.task["version"], self.card(), verify_task=self.verify, advance_task=self.advance)
        self.assertEqual(2, saved["revision"])
        self.assertEqual(3, saved["task_version"])
        with self.assertRaises(StoreError) as raised:
            self.delivery.save_card("task-1", 1, self.card(), verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("version_conflict", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
