"""模型验证工作者不触网的状态机测试。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_workbench.model_registry import ModelRegistry
from codex_workbench.model_validation import ModelValidationWorker


class ModelValidationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ModelRegistry(Path(self.temp.name) / "workbench.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def model(self):
        return self.registry.create(name="待验证", model_type="reasoning", base_url="https://model.invalid/v1", model="test-1",
                                    protocol="openai-chat", credential_ref="", voice="alloy")

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("后台验证未在限定时间内完成")

    def test_constructor_does_not_probe_until_started_then_verifies_once(self):
        calls = []
        worker = ModelValidationWorker(self.registry, lambda model, cancel: calls.append(model["id"]) or {
            "success": True, "code": "verified", "message": "合成 probe 已通过"})
        model = self.model()
        time.sleep(0.05)
        self.assertEqual([], calls)
        worker.start()
        worker.wake()
        self.wait_for(lambda: self.registry.get(model["id"])["validation_status"] == "verified")
        time.sleep(0.05)
        worker.close()
        self.assertEqual([model["id"]], calls)

    def test_two_workers_do_not_duplicate_one_claim(self):
        calls, entered, release = [], threading.Event(), threading.Event()
        def probe(model, cancel):
            calls.append(model["id"])
            entered.set()
            release.wait(1)
            return {"success": True}
        first = ModelValidationWorker(self.registry, probe)
        second = ModelValidationWorker(self.registry, probe)
        model = self.model()
        first.start(); second.start(); first.wake(); second.wake()
        self.assertTrue(entered.wait(1))
        time.sleep(0.1)
        self.assertEqual([model["id"]], calls)
        release.set()
        self.wait_for(lambda: self.registry.get(model["id"])["validation_status"] == "verified")
        first.close(); second.close()

    def test_failed_probe_waits_for_manual_queue(self):
        outcomes = [{"success": False, "code": "offline", "message": "暂不可用"}, {"success": True}]
        worker = ModelValidationWorker(self.registry, lambda model, cancel: outcomes.pop(0))
        model = self.model()
        worker.start(); worker.wake()
        self.wait_for(lambda: self.registry.get(model["id"])["validation_status"] == "failed")
        time.sleep(0.1)
        self.assertEqual(1, len(outcomes))
        self.registry.queue(model["id"])
        worker.wake()
        self.wait_for(lambda: self.registry.get(model["id"])["validation_status"] == "verified")
        worker.close()
        self.assertEqual([], outcomes)
