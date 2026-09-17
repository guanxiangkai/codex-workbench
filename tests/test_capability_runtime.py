"""运行级专业能力 MCP 的目标绑定、路径和生命周期回归。"""

import base64
import json
import hashlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_workbench.capability_manifest import create_capability_manifest, read_capability_events, revoke_capability_manifest
from codex_workbench.capability_runtime import CapabilityRuntime
from media_fixtures import PNG, JPEG, MP3, wav_bytes


class _Model(BaseHTTPRequestHandler):
    requests = []
    speech = MP3
    def log_message(self, *_): pass
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(size)
        _Model.requests.append((self.path, self.headers.get("Content-Type"), body))
        if self.headers.get("Authorization") not in {None, "Bearer target-key"}: self.send_response(401); self.end_headers(); return
        if self.path.endswith("/audio/speech"):
            payload, content_type = _Model.speech, "audio/mpeg"
        elif self.path.endswith("/audio/transcriptions"):
            payload, content_type = b'{"text":"hello"}', "application/json"
        elif self.path.endswith("/embeddings"):
            payload, content_type = b'{"data":[{"embedding":[0.1,0.2]}],"usage":{"prompt_tokens":2}}', "application/json"
        elif self.path.endswith("/rerank"):
            payload, content_type = b'{"results":[{"index":0,"relevance_score":1.0}]}', "application/json"
        else:
            payload, content_type = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":2,"completion_tokens":3}}', "application/json"
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)


class CapabilityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        _Model.requests = []
        _Model.speech = MP3
        self.root = Path(self.temp.name); self.cwd = self.root / "workspace"; self.cwd.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Model); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.server.shutdown); self.addCleanup(self.server.server_close); self.addCleanup(self.thread.join, 2)
        self.vault = self.root / "vault.sh"; self.vault.write_text("#!/bin/sh\n[ \"$1\" = exec-stdin ] || exit 64\nshift 2\nprintf target-key | \"$@\"\n"); self.vault.chmod(0o700)
        base = f"http://127.0.0.1:{self.server.server_port}/v1"
        types = ["reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"]
        self.models = [{"id": kind, "model_type": kind, "base_url": base, "model": "synthetic", "protocol": "openai-chat", "voice": "alloy", "credential_ref": "vault:target", "validation_status": "verified"} for kind in types]
        snapshots = self.root / "snapshots"; snapshots.mkdir()
        self.image = snapshots / "image.png"; self.image.write_bytes(PNG)
        self.jpeg = snapshots / "image.jpg"; self.jpeg.write_bytes(JPEG)
        self.audio = snapshots / "audio.wav"; self.audio.write_bytes(wav_bytes())
        resources = [{"snapshotPath": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (self.image, self.jpeg, self.audio)]
        self.binding = create_capability_manifest(self.root, "00000000-0000-0000-0000-000000000001", str(self.cwd), "workspace-write", self.models, resources)
        self.runtime = CapabilityRuntime(self.binding.manifest_path, self.vault)

    def test_all_six_types_are_bound_to_selected_models_and_safely_recorded(self):
        calls = [("reasoning_chat", {"model_id":"reasoning", "messages":[{"role":"user","content":"hi"}]}),
                 ("multimodal_image", {"model_id":"multimodal", "prompt":"describe", "image_path":str(self.image)}),
                 ("speech_to_text", {"model_id":"speech_to_text", "audio_path":str(self.audio)}),
                 ("text_to_speech", {"model_id":"text_to_speech", "input":"hi"}),
                 ("embedding", {"model_id":"embedding", "input":"hi"}),
                 ("rerank", {"model_id":"rerank", "query":"hi", "documents":["one"]})]
        for tool, arguments in calls:
            result = self.runtime.call(tool, arguments)
            self.assertNotIn("isError", result, (tool, result))
        self.assertNotIn("isError", self.runtime.call("multimodal_image", {"model_id":"multimodal", "prompt":"describe", "image_path":str(self.jpeg)}))
        images = [body for path, _, body in _Model.requests if path.endswith("/chat/completions")]
        self.assertTrue(any(b"data:image/png;base64," in body for body in images))
        self.assertTrue(any(b"data:image/jpeg;base64," in body for body in images))
        for body in images:
            content = json.loads(body)["messages"][0]["content"]
            if isinstance(content, list):
                encoded = content[1]["image_url"]["url"].split(",", 1)[1]
                self.assertIn(base64.b64decode(encoded, validate=True), (PNG, JPEG))
        multipart = next((item for item in _Model.requests if item[0].endswith("/audio/transcriptions")), None)
        self.assertIsNotNone(multipart)
        self.assertIn("multipart/form-data; boundary=", multipart[1])
        self.assertIn(b'filename="input.wav"', multipart[2])
        self.assertIn(b"Content-Type: audio/wav", multipart[2])
        self.assertIn(self.audio.read_bytes(), multipart[2])
        events = read_capability_events(self.binding.manifest_path)
        self.assertEqual(7, len(events)); self.assertTrue(all(item["success"] for item in events)); self.assertNotIn("target-key", repr(events))

    def test_denies_wrong_model_oversize_and_revoked_manifest(self):
        denied = self.runtime.call("embedding", {"model_id":"reasoning", "input":"x"})
        self.assertTrue(denied["isError"])
        large = self.runtime.call("embedding", {"model_id":"embedding", "input":"x" * 70000})
        self.assertTrue(large["isError"])
        revoke_capability_manifest(self.binding.manifest_path)
        self.assertTrue(self.runtime.call("embedding", {"model_id":"embedding", "input":"x"})["isError"])

    def test_rejected_model_or_unknown_tool_never_persist_untrusted_identifier(self):
        secret = "Bearer-synthetic-secret"
        self.assertTrue(self.runtime.call("embedding", {"model_id": secret, "input": "x"})["isError"])
        self.assertTrue(self.runtime.call("unknown_tool", {"model_id": secret})["isError"])
        events = read_capability_events(self.binding.manifest_path)
        self.assertEqual(["", ""], [item["model_id"] for item in events])
        self.assertEqual(["embedding", "invalid"], [item["tool"] for item in events])
        self.assertNotIn(secret, repr(events))

    def test_rejects_symlink_and_outside_input(self):
        outside = self.root / "outside.wav"; outside.write_bytes(wav_bytes())
        link = self.cwd / "link"; link.symlink_to(outside)
        self.assertTrue(self.runtime.call("speech_to_text", {"model_id":"speech_to_text", "audio_path":str(link)})["isError"])

    def test_unselected_workspace_secret_is_rejected_and_tts_artifact_can_be_transcribed(self):
        secret = self.cwd / ".env"; secret.write_text("API_KEY=synthetic-secret")
        self.assertTrue(self.runtime.call("speech_to_text", {"model_id":"speech_to_text", "audio_path":str(secret)})["isError"])
        generated = self.runtime.call("text_to_speech", {"model_id":"text_to_speech", "input":"hello"})
        self.assertNotIn("isError", generated)
        self.assertTrue(self.runtime.call("speech_to_text", {"model_id":"speech_to_text", "audio_path":generated["artifact_path"]}).get("text"))
        self.assertEqual(MP3, Path(generated["artifact_path"]).read_bytes())
        self.assertIn(MP3, _Model.requests[-1][2])

    def test_invalid_tts_never_creates_or_registers_artifacts(self):
        manifest = json.loads(Path(self.binding.manifest_path).read_text())
        directory = Path(manifest["artifacts_dir"])
        ledger = Path(manifest["artifacts_path"])
        before = sorted(directory.rglob("*"))
        for payload in (b"ID3", b"ID3audio", MP3[:-1], wav_bytes()):
            with self.subTest(size=len(payload)):
                _Model.speech = payload
                result = self.runtime.call("text_to_speech", {"model_id": "text_to_speech", "input": "hello"})
                self.assertTrue(result["isError"])
                self.assertIn("response_invalid", result["content"][0]["text"])
                self.assertEqual(before, sorted(directory.rglob("*")))
                self.assertFalse(ledger.exists() and ledger.read_bytes())
        self.assertTrue(all(not event["success"] for event in read_capability_events(self.binding.manifest_path)))

    def test_snapshot_bytes_changed_after_path_check_are_rejected_before_http(self):
        original = self.runtime._input_path

        def replace_after_check(source, manifest):
            path = original(source, manifest)
            path.write_bytes(wav_bytes(b"\x01\0" * 32))
            return path

        with patch.object(self.runtime, "_input_path", side_effect=replace_after_check):
            result = self.runtime.call("speech_to_text", {"model_id": "speech_to_text", "audio_path": str(self.audio)})
        self.assertTrue(result["isError"])
        self.assertEqual([], _Model.requests)

    def test_artifact_bytes_changed_after_path_check_are_rejected_before_http(self):
        generated = self.runtime.call("text_to_speech", {"model_id": "text_to_speech", "input": "hello"})
        self.assertNotIn("isError", generated)
        original = self.runtime._input_path

        def replace_after_check(source, manifest):
            path = original(source, manifest)
            # 合法的三帧 MP3，必须由实际字节摘要拒绝，而非格式检查拒绝。
            path.write_bytes(MP3 + MP3[:417])
            return path

        requests = len(_Model.requests)
        with patch.object(self.runtime, "_input_path", side_effect=replace_after_check):
            result = self.runtime.call("speech_to_text", {"model_id": "speech_to_text", "audio_path": generated["artifact_path"]})
        self.assertTrue(result["isError"])
        self.assertEqual(requests, len(_Model.requests))

    def test_vault_key_target_must_match_manifest_endpoint(self):
        wrong = self.root / "wrong-target-vault.sh"
        wrong.write_text("#!/bin/sh\n[ \"$1\" = exec-stdin ] || exit 64\nshift 2\nprintf '%s' '{\"api_key\":\"target-key\",\"target\":{\"base_url\":\"http://127.0.0.1:1/v1\"}}' | \"$@\"\n")
        wrong.chmod(0o700)
        result = CapabilityRuntime(self.binding.manifest_path, wrong).call("embedding", {"model_id":"embedding", "input":"x"})
        self.assertTrue(result["isError"])

    def test_stdio_exposes_selected_schema_and_completes_a_tool_call(self):
        anonymous = [{**self.models[0], "credential_ref": ""}]
        binding = create_capability_manifest(self.root, "00000000-0000-0000-0000-000000000002", str(self.cwd), "workspace-write", anonymous)
        entry = Path(__file__).resolve().parents[1] / "src" / "codex_workbench" / "capability_runtime.py"
        process = subprocess.Popen([sys.executable, str(entry), binding.manifest_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for message in (
                {"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2024-11-05"}},
                {"jsonrpc":"2.0", "id":2, "method":"tools/list"},
                {"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{"name":"reasoning_chat", "arguments":{"model_id":"reasoning", "messages":[{"role":"user", "content":"hi"}]}}},
            ):
                process.stdin.write(json.dumps(message) + "\n"); process.stdin.flush()
                response = json.loads(process.stdout.readline())
                self.assertIn("result", response)
                if message["id"] == 2:
                    tools = response["result"]["tools"]
                    self.assertEqual(["reasoning_chat"], [tool["name"] for tool in tools])
                    schema = tools[0]["inputSchema"]
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(["reasoning"], schema["properties"]["model_id"]["enum"])
                    self.assertEqual(["role", "content"], schema["properties"]["messages"]["items"]["required"])
                    self.assertEqual({"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}, tools[0]["annotations"])
                if message["id"] == 3:
                    self.assertIn("ok", response["result"]["content"][0]["text"])
        finally:
            process.terminate(); process.wait(3)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()

    def test_read_only_tts_is_non_readonly_and_fixed_rejected(self):
        binding = create_capability_manifest(self.root, "00000000-0000-0000-0000-000000000003", str(self.cwd), "read-only", [self.models[3]])
        runtime = CapabilityRuntime(binding.manifest_path, self.vault)
        tool = runtime.tools()[0]
        self.assertEqual({"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}, tool["annotations"])
        result = runtime.call("text_to_speech", {"model_id":"text_to_speech", "input":"hello"})
        self.assertTrue(result["isError"])
        self.assertIn("read_only_tts_unavailable", result["content"][0]["text"])


if __name__ == "__main__": unittest.main()
