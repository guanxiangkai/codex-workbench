"""独立模型登记和绑定门禁的合成数据库测试。"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
import sqlite3
from pathlib import Path

from codex_workbench.model_registry import ModelRegistry, ModelRegistryError


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ModelRegistry(Path(self.temp.name) / "workbench.sqlite3")
        self.assistant_id = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def model(self, **changes):
        fields = {"name": "测试模型", "model_type": "reasoning", "base_url": "https://model.invalid/v1", "model": "test-1",
                  "protocol": "openai-chat", "credential_ref": "vault:model-test", "voice": "alloy"}
        return self.registry.create(**(fields | changes))

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(ModelRegistryError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def verify(self, model, now=None):
        now = time.time() if now is None else now
        claimed = self.registry.claim_due("test-worker", now=now)
        self.assertEqual(model["id"], claimed["id"])
        return self.registry.finish(model["id"], claimed["version"], claimed["lease_token"], True, now=now + 1)

    def test_create_validate_update_and_safe_input_contract(self):
        model = self.model()
        self.assertEqual("pending", model["validation_status"])
        self.assertEqual(1, model["version"])
        defaults = self.registry.create(name="默认音频", model_type="speech_to_text", base_url="http://127.0.0.1:8000/v1", model="stt")
        self.assertEqual(("openai-chat", "", "alloy"), (defaults["protocol"], defaults["credential_ref"], defaults["voice"]))
        rerank = self.model(name="重排序", model_type="rerank", model="rerank-1")
        self.assertEqual("rerank", rerank["model_type"])
        verified = self.verify(model)
        self.assertEqual("verified", verified["validation_status"])
        self.assertIsNone(verified["next_check_at"])
        updated = self.registry.update(model["id"], verified["version"], name="改名模型")
        self.assertEqual("pending", updated["validation_status"])
        self.assertEqual(2, updated["version"])
        for changes in ({"base_url": "https://user:password@model.invalid/v1"}, {"base_url": "https://model.invalid/v1?token=bad"},
                        {"credential_ref": "sk-not-a-reference"}, {"model_type": "unconfigured"},
                        {"model_type": "embedding", "protocol": "openai-responses"},
                        {"model_type": "rerank", "protocol": "openai-responses"}):
            with self.subTest(changes=changes):
                self.assert_error("validation", self.model, **changes)

    def test_claim_is_exclusive_and_late_finish_cannot_overwrite_new_lease(self):
        model = self.model()
        now = time.time()
        first = self.registry.claim_due("one", now=now)
        self.assertIsNone(self.registry.claim_due("two", now=now))
        self.assert_error("conflict", self.registry.queue, model["id"])
        self.assertEqual(first["lease_token"], self.registry.get(model["id"])["lease_token"])
        self.assertIsNone(self.registry.claim_due("two", now=now + 61))
        interrupted = self.registry.get(model["id"])
        self.assertEqual("failed", interrupted["validation_status"])
        self.assertEqual("验证中断，请手动重新验证", interrupted["last_error"])
        self.assertIsNone(self.registry.finish(model["id"], first["version"], first["lease_token"], True, now=now + 62))
        self.registry.queue(model["id"])
        recovered = self.registry.claim_due("two", now=now + 62)
        finished = self.registry.finish(model["id"], recovered["version"], recovered["lease_token"], True, now=now + 63)
        self.assertEqual("verified", finished["validation_status"])

    def test_expired_lease_result_cannot_complete_before_reclaim(self):
        model = self.model()
        now = time.time()
        claim = self.registry.claim_due("worker", now=now)
        self.assertIsNone(self.registry.finish(model["id"], claim["version"], claim["lease_token"], True, now=now + 61))
        self.assertIsNone(self.registry.claim_due("worker", now=now + 61))
        self.assertEqual("failed", self.registry.get(model["id"])["validation_status"])

    def test_failure_requires_manual_queue_and_pause_resume_verify_once(self):
        model = self.model()
        now = time.time()
        claim = self.registry.claim_due("worker", now=now)
        failed = self.registry.finish(model["id"], claim["version"], claim["lease_token"], False, "unavailable", "服务不可用", now=now + 1)
        self.assertEqual("failed", failed["validation_status"])
        self.assertIsNone(failed["next_check_at"])
        self.assertIsNone(self.registry.claim_due("worker", now=now + 3600))
        queued = self.registry.queue(model["id"])
        self.assertEqual("pending", queued["validation_status"])
        self.assertIsNone(queued["next_check_at"])
        self.assertEqual(model["id"], self.registry.claim_due("worker", now=now + 2)["id"])
        paused = self.registry.set_paused(model["id"], True)
        self.assertEqual("paused", paused["validation_status"])
        self.assert_error("conflict", self.registry.queue, model["id"])
        resumed = self.registry.set_paused(model["id"], False)
        self.assertEqual("pending", resumed["validation_status"])
        self.assertIsNone(resumed["next_check_at"])

    def test_initialization_clears_legacy_failure_schedule(self):
        model = self.model()
        with sqlite3.connect(self.registry.path) as conn:
            conn.execute("UPDATE provider_models SET validation_status='failed',next_check_at=1234567890 WHERE id=?", (model["id"],))
        restored = ModelRegistry(self.registry.path)
        self.assertIsNone(restored.get(model["id"])["next_check_at"])
        self.assertIsNone(restored.claim_due("worker", now=2_000_000_000))

    def test_existing_database_migrates_model_type_constraint_for_rerank(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        model_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE provider_models(
                id TEXT PRIMARY KEY,name TEXT NOT NULL,model_type TEXT NOT NULL CHECK(model_type IN ('reasoning','multimodal','speech_to_text','text_to_speech','embedding','unconfigured')),
                base_url TEXT NOT NULL,model TEXT NOT NULL,protocol TEXT NOT NULL,credential_ref TEXT NOT NULL,voice TEXT NOT NULL,version INTEGER NOT NULL,
                validation_status TEXT NOT NULL,last_checked_at REAL,last_verified_at REAL,next_check_at REAL,attempt_count INTEGER NOT NULL,
                last_error_code TEXT,last_error TEXT,lease_token TEXT,lease_until REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL,legacy_fingerprint TEXT UNIQUE
            )""")
            conn.execute("CREATE TABLE assistant_model_bindings(assistant_id TEXT NOT NULL,model_id TEXT NOT NULL REFERENCES provider_models(id),PRIMARY KEY(assistant_id,model_id))")
            conn.execute("INSERT INTO provider_models VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (model_id,"旧模型","reasoning","https://model.invalid/v1","old","openai-chat","","alloy",1,"verified",None,1,None,1,None,None,None,None,1.0,1.0,None))
            conn.execute("INSERT INTO assistant_model_bindings VALUES (?,?)", (assistant_id,model_id))
        migrated = ModelRegistry(path)
        self.assertEqual([model_id], migrated.bindings(assistant_id))
        self.assertEqual("rerank", migrated.create(name="重排序", model_type="rerank", base_url="https://model.invalid/v1", model="rerank-test")["model_type"])

    def test_bindings_require_verified_and_become_invalid_after_edit(self):
        model = self.model()
        self.assert_error("validation", self.registry.set_bindings, self.assistant_id, [model["id"]])
        verified = self.verify(model)
        self.assertEqual([model["id"]], self.registry.set_bindings(self.assistant_id, [model["id"]]))
        self.assertEqual([model["id"]], self.registry.bindings(self.assistant_id))
        self.assertEqual([model["id"]], [record["id"] for record in self.registry.selected_models(self.assistant_id)])
        self.registry.update(model["id"], verified["version"], model="test-2")
        self.assert_error("validation", self.registry.selected_models, self.assistant_id)
        self.assertEqual("pending", self.registry.selected_models(self.assistant_id, require_verified=False)[0]["validation_status"])

    def test_normalized_endpoint_and_model_are_unique_without_deleting_history(self):
        first = self.model(base_url="https://MODEL.invalid/v1/")
        for endpoint in ("https://model.invalid/v1/chat/completions",):
            with self.subTest(endpoint=endpoint):
                self.assert_error("duplicate_endpoint", self.model, base_url=endpoint)
        changed = self.registry.update(first["id"], first["version"], base_url="https://model.invalid/v1/chat/completions")
        self.assertEqual("https://model.invalid/v1/chat/completions", changed["base_url"])
        self.assert_error("validation", self.model, model="test-2", base_url="https://model.invalid/v1/embeddings")
        rerank = self.model(model="test-2", model_type="rerank", base_url="https://model.invalid/v1")
        self.assertTrue(rerank["effective_endpoint"].endswith("/v1/rerank"))

    def test_endpoint_preflight_returns_safe_effective_endpoint_before_storage(self):
        first = self.model()
        config = {key: first[key] for key in ("name", "model_type", "base_url", "model", "protocol", "credential_ref", "voice")}
        self.assertTrue(self.registry.check_endpoint(config, exclude_id=first["id"]).endswith("/v1/chat/completions"))
        self.assert_error("duplicate_endpoint", self.registry.check_endpoint, config)

    def test_set_bindings_joins_caller_sqlite_transaction(self):
        model = self.model()
        self.verify(model)
        conn = sqlite3.connect(self.registry.path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.registry.set_bindings(self.assistant_id, [model["id"]], connection=conn)
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual([], self.registry.bindings(self.assistant_id))

    def test_legacy_services_are_paused_unconfigured_and_idempotent(self):
        service = {"name": "旧服务", "base_url": "http://127.0.0.1:8000/v1", "model": "", "credential_ref": "vault:old-model"}
        first = self.registry.import_legacy(self.assistant_id, [service])
        second = self.registry.import_legacy(self.assistant_id, [service])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual("unconfigured", first[0]["model_type"])
        self.assertEqual("paused", first[0]["validation_status"])
        self.assertEqual([], self.registry.bindings(self.assistant_id))
