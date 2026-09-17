"""使用合成数据库、事件屏障与回环 HTTP 复现模型状态竞态。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_workbench.model_probe import probe_model
from codex_workbench.model_registry import ModelRegistry, ModelRegistryError
from codex_workbench.model_validation import ModelValidationWorker


class ModelStateEdgesTests(unittest.TestCase):
    """验证用户意图、新配置和租约截止时间在交错操作下仍有效。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = ModelRegistry(Path(self.temp.name) / "models.sqlite3")
        self.assistant_id = str(uuid.uuid4())

    def model(self, **changes):
        return self.registry.create(**({"name": "合成模型", "model_type": "reasoning",
                                       "base_url": "http://127.0.0.1:9/v1", "model": "synthetic"} | changes))

    def finish(self, claim, success, **kwargs):
        return self.registry.finish(claim["id"], claim["version"], claim["lease_token"], success, **kwargs)

    def assert_unavailable(self, model_id):
        self.assertEqual([model_id], self.registry.bindings(self.assistant_id))
        with self.assertRaises(ModelRegistryError):
            self.registry.selected_models(self.assistant_id)
        with self.assertRaises(ModelRegistryError):
            self.registry.validate_ids([model_id])

    def test_pause_survives_edit_in_both_orders_and_rejects_old_success(self):
        model = self.model()
        self.finish(self.registry.claim_due("initial"), True)
        self.registry.set_bindings(self.assistant_id, [model["id"]])
        for field, value in (("credential_ref", "vault:synthetic-new"),
                             ("base_url", "http://127.0.0.1:9/new/v1")):
            for pause_first in (True, False):
                with self.subTest(field=field, pause_first=pause_first):
                    self.registry.set_paused(model["id"], False)
                    old = self.registry.claim_due("old")
                    if pause_first:
                        self.registry.set_paused(model["id"], True)
                    edited = self.registry.update(model["id"], old["version"], **{field: value})
                    if not pause_first:
                        self.registry.set_paused(model["id"], True)
                    self.assertIsNone(self.finish(old, True))
                    current = self.registry.get(model["id"])
                    self.assertEqual("paused", current["validation_status"])
                    self.assertEqual(edited["version"], current["version"])
                    self.assertIsNone(self.registry.claim_due("unexpected"))
                    self.assert_unavailable(model["id"])

    def test_old_success_and_failure_cannot_replace_new_validation(self):
        model = self.model()
        for old_success in (True, False):
            with self.subTest(old_success=old_success):
                self.registry.queue(model["id"])
                old = self.registry.claim_due("old")
                changed = self.registry.update(model["id"], old["version"],
                                               credential_ref="vault:synthetic-replacement",
                                               base_url="http://127.0.0.1:9/replacement/v1")
                fresh = self.registry.claim_due("fresh")
                self.assertIsNone(self.finish(old, old_success))
                self.assertEqual(fresh["lease_token"], self.registry.get(model["id"])["lease_token"])
                accepted = self.finish(fresh, not old_success)
                self.assertIsNone(self.finish(old, old_success))
                current = self.registry.get(model["id"])
                self.assertEqual(accepted["validation_status"], current["validation_status"])
                self.assertEqual(changed["version"], current["version"])
                self.assertEqual(changed["credential_ref"], current["credential_ref"])
                self.assertEqual(changed["base_url"], current["base_url"])

    def test_exact_lease_deadline_rejects_result_before_or_after_reclaim(self):
        model = self.model()
        for success in (True, False):
            for reclaim_first in (True, False):
                with self.subTest(success=success, reclaim_first=reclaim_first):
                    self.registry.set_paused(model["id"], False)
                    claim = self.registry.claim_due("expired", now=1000)
                    if reclaim_first:
                        self.assertIsNone(self.registry.claim_due("reclaimer", now=1060))
                    self.assertIsNone(self.finish(claim, success, now=1060))
                    self.assertIsNone(self.registry.claim_due("reclaimer", now=1060))
                    self.assertEqual("validation_interrupted", self.registry.get(model["id"])["last_error_code"])

    def test_lease_clock_is_sampled_after_waiting_for_sqlite_writer(self):
        model = self.model()
        for operation in ("finish", "claim_due"):
            with self.subTest(operation=operation):
                self.registry.set_paused(model["id"], False)
                claim = self.registry.claim_due("old", now=1000)
                clock, entered = [1059.0], threading.Event()
                connection = self.registry._connection

                @contextmanager
                def observed_connection(*args, **kwargs):
                    entered.set()
                    with connection(*args, **kwargs) as conn:
                        yield conn

                writer = sqlite3.connect(self.registry.path, isolation_level=None)
                writer.execute("BEGIN IMMEDIATE")
                try:
                    with patch.object(self.registry, "_clock", side_effect=lambda now: clock[0]), \
                            patch.object(self.registry, "_connection", observed_connection), \
                            ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(self.finish, claim, True) if operation == "finish" else pool.submit(self.registry.claim_due, "new")
                        try:
                            self.assertTrue(entered.wait(2), "操作未到达写事务边界")
                        finally:
                            clock[0] = 1061.0
                            writer.rollback()
                        self.assertIsNone(pending.result(timeout=2))
                finally:
                    writer.close()
                if operation == "claim_due":
                    self.assertEqual("failed", self.registry.get(model["id"])["validation_status"])

    def test_inflight_probe_success_cannot_revive_edited_paused_model(self):
        entered, release = threading.Event(), threading.Event()

        def probe(config, cancel):
            entered.set()
            release.wait(3)
            return {"success": True}

        self.check_inflight_pause(probe, entered, release, "http://127.0.0.1:9")

    def test_inflight_http_success_cannot_revive_edited_paused_model(self):
        entered, release = threading.Event(), threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                entered.set()
                release.wait(3)
                body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("当前沙箱禁止绑定回环端口；事件屏障 probe 用例独立覆盖工作者竞态")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.check_inflight_pause(lambda config, cancel: probe_model(config, cancel_event=cancel),
                                      entered, release, f"http://127.0.0.1:{server.server_port}")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(["/v1/chat/completions"], requests)

    def check_inflight_pause(self, probe, entered, release, base):
        finished, outcomes = threading.Event(), []
        model = self.model(base_url=base + "/v1")
        self.finish(self.registry.claim_due("initial"), True)
        self.registry.set_bindings(self.assistant_id, [model["id"]])
        self.registry.queue(model["id"])
        original_finish = self.registry.finish

        def observed_finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            outcomes.append(result)
            finished.set()
            return result

        worker = ModelValidationWorker(self.registry, probe)
        try:
            with patch.object(self.registry, "finish", side_effect=observed_finish):
                worker.start()
                self.assertTrue(entered.wait(2), "验证请求未进入屏障")
                self.registry.set_paused(model["id"], True)
                self.registry.update(model["id"], model["version"], base_url=base + "/new/v1")
                release.set()
                self.assertTrue(finished.wait(2), "旧请求未完成")
                self.assertEqual([None], outcomes)
                self.assertEqual("paused", self.registry.get(model["id"])["validation_status"])
                self.assert_unavailable(model["id"])
        finally:
            release.set()
            self.assertTrue(worker.close(), "验证工作者未停止")
