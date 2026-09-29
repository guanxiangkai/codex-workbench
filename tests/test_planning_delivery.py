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
        self.task = {"id": "task-1", "version": 1}
        self.asset = {"id": "asset-1", "sha256": "a" * 64}
        self.run = {"id": "run-1", "task_id": "task-1"}
        self.message = {"id": "message-1", "task_id": "task-1"}
        self.db.execute("INSERT INTO assets VALUES(?,?)", (self.asset["id"], self.asset["sha256"]))
        self.db.execute("INSERT INTO asset_refs VALUES(?,?)", (self.asset["id"], self.task["id"]))
        self.db.execute("INSERT INTO runs VALUES(?,?,?)", (self.run["id"], self.task["id"], "review"))
        self.delivery.save_card("task-1", self.task["version"], self.card(), verify_task=self.verify, advance_task=self.advance)

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

    def test_card_uses_parent_version_contract(self):
        saved = self.delivery.save_card("task-1", self.task["version"], self.card(), verify_task=self.verify, advance_task=self.advance)
        self.assertEqual(2, saved["revision"])
        self.assertEqual(3, saved["task_version"])
        with self.assertRaises(StoreError) as raised:
            self.delivery.save_card("task-1", 1, self.card(), verify_task=self.verify, advance_task=self.advance)
        self.assertEqual("version_conflict", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
