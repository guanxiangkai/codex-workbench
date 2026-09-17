"""受控子进程模型探测的凭据和生命周期边界测试。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_workbench.model_probe_runner import ModelProbeRunner
from codex_workbench.model_probe_worker import decode_credential


class _ProbeHandler(BaseHTTPRequestHandler):
    """只接受回环请求，记录认证头以验证 vault 到 worker 的单向 stdin。"""

    authorization = None
    requested = threading.Event()
    slow = False

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        type(self).authorization = self.headers.get("Authorization")
        type(self).requested.set()
        if type(self).slow:
            time.sleep(10)
        body = b'{"choices":[{"message":{"content":"OK"}}]}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, format, *args) -> None:  # noqa: A003
        pass


class ModelProbeRunnerTest(unittest.TestCase):
    """使用临时 vault 脚本及本机 HTTP 服务，禁止真实认证或模型调用。"""

    def setUp(self) -> None:
        _ProbeHandler.authorization = None
        _ProbeHandler.requested = threading.Event()
        _ProbeHandler.slow = False
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.reference_file = self.root / "vault-reference.txt"
        self.vault = self.root / "fake-vault.sh"
        self.vault.write_text(
            "#!/bin/sh\n"
            "[ \"$1\" = exec-stdin ] || exit 64\n"
            "printf '%s' \"$2\" > \"$FAKE_REF_PATH\"\n"
            "shift 2\n"
            "printf '%s' \"$FAKE_PROBE_CREDENTIAL\" | \"$@\"\n",
            encoding="utf-8",
        )
        self.vault.chmod(0o700)
        self.model = {
            "name": "本机回环", "model_type": "reasoning",
            "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "model": "synthetic", "protocol": "openai-chat",
            "credential_ref": "vault:loopback-model", "voice": "alloy",
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def runner(self, timeout: float = 2) -> ModelProbeRunner:
        return ModelProbeRunner(self.root / "data", vault_command=self.vault, timeout=timeout)

    def wait_for_request_or_result(self, thread: threading.Thread, result_box: list[dict], seconds: float) -> None:
        """等待真正的阻塞网络请求；子进程提前结束时立即提供其安全结果。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if _ProbeHandler.requested.wait(0.02):
                return
            if not thread.is_alive():
                self.fail(f"探测在发出回环请求前结束：{result_box!r}")
        self.fail("探测子进程未在启动边界内发出回环请求")

    def test_raw_and_json_credentials_normalize_to_one_in_memory_token(self) -> None:
        self.assertEqual("raw-key", decode_credential(b" Bearer raw-key \n"))
        self.assertEqual("json-key", decode_credential(b'{"api_key":"json-key"}'))
        scoped = b'{"api_key":"json-key","target":{"base_url":"http://127.0.0.1:9000/v1/"}}'
        self.assertEqual("json-key", decode_credential(scoped, "http://127.0.0.1:9000/v1"))
        with self.assertRaisesRegex(ValueError, "credential_target"):
            decode_credential(scoped, "http://127.0.0.1:9001/v1")
        with self.assertRaises(ValueError):
            decode_credential(b'{"api_key":"a","token":"b"}')

    def test_fake_vault_passes_json_to_worker_without_inheriting_openai_key(self) -> None:
        environment = {
            "FAKE_REF_PATH": str(self.reference_file),
            "FAKE_PROBE_CREDENTIAL": '{"api_key":"vault-test-token"}',
            "OPENAI_API_KEY": "ambient-test-token",
        }
        with patch.dict(os.environ, environment, clear=False):
            result = self.runner()(self.model, threading.Event())
        self.assertTrue(result["success"])
        self.assertEqual("verified", result["code"])
        self.assertEqual("loopback-model", self.reference_file.read_text(encoding="utf-8"))
        self.assertEqual("Bearer vault-test-token", _ProbeHandler.authorization)
        self.assertNotIn("vault-test-token", repr(result))
        self.assertNotIn("ambient-test-token", repr(result))
        self.assertEqual([], list((self.root / "data" / "model-probes").glob("probe-*.json")))
        with patch.dict(os.environ, {"FAKE_REF_PATH": str(self.reference_file), "FAKE_PROBE_CREDENTIAL": "raw-vault-token"}, clear=False):
            raw_result = self.runner()(self.model, threading.Event())
        self.assertTrue(raw_result["success"])
        self.assertEqual("Bearer raw-vault-token", _ProbeHandler.authorization)
        self.assertNotIn("raw-vault-token", repr(raw_result))

    def test_cancel_and_timeout_close_control_pipe_and_clean_temporary_config(self) -> None:
        _ProbeHandler.slow = True
        cancellation = threading.Event()
        result_box: list[dict] = []
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_REF_PATH": str(self.reference_file), "FAKE_PROBE_CREDENTIAL": "token"}, clear=False):
            # 取消验证采用接近生产值的硬时限；短时限应由独立用例验证，
            # 否则进程启动竞争会把“尚未发出请求”误认为取消能力失败。
            thread = threading.Thread(target=lambda: result_box.append(self.runner(timeout=10)(self.model, cancellation)))
            thread.start()
            self.wait_for_request_or_result(thread, result_box, 5)
            cancellation.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual("cancelled", result_box[0]["code"])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([], list((self.root / "data" / "model-probes").glob("probe-*.json")))

        # 0.2 秒硬上限覆盖 worker 尚在启动以及已经进入阻塞请求两种状态；
        # 因此不要求回环服务已收到请求，避免把启动竞争写成网络断言。
        _ProbeHandler.requested.clear()
        result_box.clear()
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_REF_PATH": str(self.reference_file), "FAKE_PROBE_CREDENTIAL": "token"}, clear=False):
            thread = threading.Thread(target=lambda: result_box.append(self.runner(timeout=0.2)(self.model, threading.Event())))
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual("timeout", result_box[0]["code"])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([], list((self.root / "data" / "model-probes").glob("probe-*.json")))

    def test_invalid_reference_and_pre_cancelled_request_do_not_run_vault(self) -> None:
        self.assertEqual("credential_reference", self.runner()({**self.model, "credential_ref": "bad-value"}, threading.Event())["code"])
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual("cancelled", self.runner()(self.model, cancelled)["code"])
        self.assertFalse(self.reference_file.exists())


if __name__ == "__main__":
    unittest.main()
