"""账户默认值与身份缓存回归；所有 RPC、账户与目录均为合成数据。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import queue
import tempfile
import time
import unittest

from codex_workbench.account_service import AccountService
from codex_workbench.codex_rpc import RpcError


class AccountDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.identity = {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "pro"}
        self.subject = "subject-a"
        self.config = {}
        self.models = [
            {"id": "default-id", "model": "default-model", "isDefault": True, "defaultReasoningEffort": "medium"},
            {"id": "selected-id", "model": "selected-model", "isDefault": False, "defaultReasoningEffort": "high"},
        ]
        self.calls = []
        self.failures = set()
        self.after_request = lambda method: None
        case = self

        class FakeRpc:
            def __init__(self, *args, **kwargs):
                self.events = queue.Queue()

            def request(self, method, params=None):
                case.calls.append(method)
                if method in case.failures:
                    raise RpcError("合成 RPC 失败")
                if method == "account/read":
                    result = {"account": case.identity}
                elif method == "account/rateLimits/read":
                    result = {"accountId": case.subject, "rateLimits": {"limitId": "codex"}}
                elif method == "config/read":
                    result = {"config": case.config}
                elif method == "model/list":
                    result = {"data": case.models}
                else:
                    raise AssertionError("禁止调用非只读 RPC: " + method)
                result = deepcopy(result)
                case.after_request(method)
                return result

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        self.addCleanup(patch.stopall)
        patch("codex_workbench.account_service.AccountRpc", FakeRpc).start()
        patch("codex_workbench.account_service.current_account_home", return_value=str(self.root)).start()
        store = SimpleNamespace(register_current_account=lambda name, directory, subject: {
            "id": "current", "name": name, "codex_home": directory, "subject_id": subject,
        })
        self.service = AccountService(store, self.root / "accounts", "unused", self.root)
        self.addCleanup(self.service.close)

    def assert_defaults(self, model, effort):
        current = self.service.current(refresh=True)
        self.assertEqual("ready", current["login"]["status"])
        defaults = self.service.system_defaults()
        self.assertEqual(current["taskDefaults"], defaults)
        self.assertEqual((model, effort), (defaults["model"], defaults["effort"]))

    def test_empty_config_uses_catalog_default_for_both_entry_points(self):
        self.assert_defaults("default-model", "medium")

    def test_configured_model_uses_its_own_effort_by_model_or_id(self):
        for model in ("selected-model", "selected-id"):
            with self.subTest(model=model):
                self.config = {"model": model}
                self.assert_defaults(model, "high")

    def test_explicit_effort_overrides_catalog(self):
        self.config = {"model": "selected-model", "model_reasoning_effort": "low"}
        self.assert_defaults("selected-model", "low")

    def test_effort_only_config_keeps_effort_and_uses_catalog_model(self):
        self.config = {"model_reasoning_effort": "low"}
        self.assert_defaults("default-model", "low")

    def test_unlisted_model_never_borrows_another_models_effort(self):
        self.config = {"model": "unlisted-model"}
        self.assert_defaults("unlisted-model", None)

    def test_catalog_without_default_does_not_invent_model_or_effort(self):
        for model in self.models:
            model["isDefault"] = False
        self.assert_defaults(None, None)

    def test_refresh_reloads_config_and_catalog(self):
        self.assertEqual("default-model", self.service.current()["taskDefaults"]["model"])
        self.config = {"model": "selected-model"}
        self.models[1]["defaultReasoningEffort"] = "low"
        self.assert_defaults("selected-model", "low")

    def test_expired_current_cache_reads_fresh_config_with_cached_catalog(self):
        self.service.current()
        self.service.current_cache["time"] = time.monotonic() - 46
        self.config = {"model": "selected-model"}
        value = self.service.current()
        self.assertEqual(("selected-model", "high"), (value["taskDefaults"]["model"], value["taskDefaults"]["effort"]))
        self.assertEqual(1, self.calls.count("model/list"))

    def test_system_defaults_do_not_require_login_or_usage(self):
        self.identity = None
        self.failures.add("account/rateLimits/read")
        defaults = self.service.system_defaults()
        self.assertEqual("default-model", defaults["model"])
        self.assertNotIn("account/read", self.calls)
        self.assertNotIn("account/rateLimits/read", self.calls)

    def test_complete_config_does_not_require_model_catalog(self):
        self.config = {"model": "configured-model", "model_reasoning_effort": "low", "agents": {"max_threads": 3}}
        self.failures.add("model/list")
        defaults = self.service.system_defaults()
        self.assertEqual(("configured-model", "low", 3), (defaults["model"], defaults["effort"], defaults["concurrency"]))
        self.assertNotIn("model/list", self.calls)

    def test_new_settings_read_invalidates_previous_login_and_model_projection(self):
        self.service.current()
        self.subject = "subject-b"
        self.models = [{"id": "new", "model": "new", "isDefault": True, "defaultReasoningEffort": "high"}]
        self.assertEqual("new", self.service.system_defaults()["model"])
        self.assertIsNone(self.service.current_cache)
        self.assertIsNone(self.service.model_cache)
        value = self.service.current()
        self.assertEqual(("subject-b", "new"), (value["identity_id"], value["taskDefaults"]["model"]))

    def test_failed_settings_read_drops_previous_projections(self):
        self.service.current()
        self.failures.add("config/read")
        with self.assertRaises(RpcError):
            self.service.system_defaults()
        self.assertIsNone(self.service.current_cache)
        self.assertIsNone(self.service.model_cache)

    def test_identity_or_subject_change_reloads_model_cache_without_recursion(self):
        for changed in ("subject", "identity"):
            with self.subTest(changed=changed):
                self.service.current(refresh=True)
                before = self.calls.count("model/list")
                if changed == "subject":
                    self.subject = "subject-b"
                else:
                    self.identity["planType"] = "plus"
                self.models[0]["defaultReasoningEffort"] = "low"
                self.service.current_cache["time"] = time.monotonic() - 46
                value = self.service.current()
                self.assertEqual("ready", value["login"]["status"])
                self.assertEqual("low", value["taskDefaults"]["effort"])
                self.assertEqual(before + 1, self.calls.count("model/list"))

    def test_logout_and_failed_refresh_clear_both_caches(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                self.identity = {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "pro"}
                self.service.current(refresh=True)
                if unavailable:
                    self.failures.add("account/read")
                else:
                    self.identity = None
                value = self.service.current(refresh=True)
                self.assertEqual("unavailable" if unavailable else "not_logged_in", value["login"]["status"])
                self.assertIsNone(self.service.current_cache)
                self.assertIsNone(self.service.model_cache)

    def test_identity_change_during_settings_read_is_not_published(self):
        def change_identity(method):
            if method == "config/read":
                self.identity = {"type": "chatgpt", "email": "changed@example.invalid", "planType": "pro"}
        self.after_request = change_identity
        value = self.service.current(refresh=True)
        self.assertEqual("unavailable", value["login"]["status"])
        self.assertIsNone(self.service.current_cache)
        self.assertIsNone(self.service.model_cache)

    def test_failed_isolated_account_refresh_cannot_resurrect_cached_login(self):
        account_id = "isolated"
        self.service.get = lambda identity: {"id": identity, "codex_home": str(self.root)}
        self.service.cache[account_id] = {"time": time.monotonic(), "value": {"login": {"status": "ready"}}}
        self.failures.add("account/read")
        for refresh in (True, False):
            with self.subTest(refresh=refresh), self.assertRaises(RpcError):
                self.service.status(account_id, refresh=refresh)
        self.assertNotIn(account_id, self.service.cache)

    def test_failed_login_start_invalidates_previous_login_projection(self):
        account_id = "isolated"
        self.service.get = lambda identity: {"id": identity, "codex_home": str(self.root)}
        self.service._has_running = lambda identity: False
        self.service.cache[account_id] = {"time": time.monotonic(), "value": {"login": {"status": "ready"}}}
        self.failures.add("account/login/start")
        with self.assertRaises(RpcError):
            self.service.login(account_id)
        self.assertNotIn(account_id, self.service.cache)

if __name__ == "__main__":
    unittest.main()
