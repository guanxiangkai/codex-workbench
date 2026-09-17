"""模型能力探测的本机回环契约测试，不访问真实服务。"""

from __future__ import annotations

import base64
import json
import math
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_workbench.model_probe import probe_model
from media_fixtures import MP3, wav_bytes


class _Handler(BaseHTTPRequestHandler):
    """按测试路径输出确定响应，并记录请求而不记录凭据。"""

    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
    requests: list[tuple[str, bytes]] = []
    last_accept: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append((self.path, self.rfile.read(size)))
        type(self).last_accept = self.headers.get("Accept")
        status, headers, body = type(self).routes.get(self.path, (404, {"Content-Type": "application/json"}, b"{}"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A003
        pass


class ModelProbeTest(unittest.TestCase):
    """每个场景均通过临时 HTTP 服务验证真实请求和脱敏失败语义。"""

    def setUp(self) -> None:
        _Handler.routes, _Handler.requests, _Handler.last_accept = {}, [], None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def model(self, model_type: str, protocol: str = "openai-chat") -> dict:
        return {"name": "本地测试", "model_type": model_type, "base_url": self.base, "model": "synthetic", "protocol": protocol, "voice": "alloy", "credential_ref": "vault:synthetic"}

    @staticmethod
    def response(value: dict) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()

    def test_reasoning_chat_and_responses_use_minimal_real_requests(self) -> None:
        _Handler.routes["/v1/chat/completions"] = self.response({"choices": [{"message": {"reasoning_content": "OK"}}]})
        self.assertTrue(probe_model(self.model("reasoning"))["success"])
        body = json.loads(_Handler.requests[-1][1])
        self.assertEqual("回复OK", body["messages"][0]["content"])
        self.assertEqual(256, body["max_completion_tokens"])
        _Handler.routes["/v1/responses"] = self.response({"output_text": "OK"})
        self.assertTrue(probe_model(self.model("reasoning", "openai-responses"))["success"])
        response_body = json.loads(_Handler.requests[-1][1])
        self.assertEqual("回复OK", response_body["input"])
        self.assertEqual(256, response_body["max_output_tokens"])
        _Handler.routes["/v1/chat/completions"] = self.response({"choices": [{"message": {"content": "OK"}}]})
        full = {**self.model("reasoning"), "base_url": self.base + "/chat/completions"}
        self.assertTrue(probe_model(full)["success"])
        self.assertEqual("/v1/chat/completions", _Handler.requests[-1][0])
        _Handler.routes["/v1/chat/completions"] = self.response({
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 241}},
        })
        chat_budget = probe_model(self.model("reasoning"))
        self.assertTrue(chat_budget["success"])
        self.assertEqual("推理接口已响应", chat_budget["message"])
        _Handler.routes["/v1/responses"] = self.response({
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"output_tokens_details": {"reasoning_tokens": 241}},
        })
        self.assertTrue(probe_model(self.model("reasoning", "openai-responses"))["success"])

    def test_multimodal_embedding_and_tts_success(self) -> None:
        _Handler.routes["/v1/chat/completions"] = self.response({"choices": [{"message": {"content": "green"}}]})
        self.assertTrue(probe_model(self.model("multimodal"))["success"])
        image = json.loads(_Handler.requests[-1][1])["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/png;base64,"))
        image_bytes = base64.b64decode(image.split(",", 1)[1], validate=True)
        self.assertEqual(b"\x89PNG\r\n\x1a\n", image_bytes[:8])
        chunks, offset = [], 8
        while offset < len(image_bytes):
            length = struct.unpack(">I", image_bytes[offset:offset + 4])[0]
            kind = image_bytes[offset + 4:offset + 8]
            payload = image_bytes[offset + 8:offset + 8 + length]
            crc = struct.unpack(">I", image_bytes[offset + 8 + length:offset + 12 + length])[0]
            self.assertEqual(zlib.crc32(kind + payload) & 0xFFFFFFFF, crc)
            chunks.append((kind, payload))
            offset += 12 + length
        self.assertEqual(len(image_bytes), offset)
        header = next(payload for kind, payload in chunks if kind == b"IHDR")
        self.assertEqual((32, 32, 8, 2, 0, 0, 0), struct.unpack(">IIBBBBB", header))
        pixels = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
        self.assertEqual(32 * (1 + 32 * 3), len(pixels))
        for row in range(32):
            start = row * 97
            self.assertEqual(b"\x00", pixels[start:start + 1])
            self.assertEqual(b"\xff\x00\x00" * 32, pixels[start + 1:start + 97])
        _Handler.routes["/v1/embeddings"] = self.response({"data": [{"embedding": [0.0, -1.25, 2]}]})
        evidence = probe_model(self.model("embedding"))
        self.assertEqual({"dimensions": 3}, evidence["evidence"])
        _Handler.routes["/v1/audio/speech"] = (200, {"Content-Type": "audio/mpeg"}, MP3)
        self.assertTrue(probe_model(self.model("text_to_speech"))["success"])
        tts_body = json.loads(_Handler.requests[-1][1])
        self.assertEqual("mp3", tts_body["response_format"])
        self.assertNotIn("format", tts_body)
        self.assertEqual("audio/*, application/json", _Handler.last_accept)

    def test_rerank_uses_standard_endpoint_and_checks_scores_and_indexes(self) -> None:
        _Handler.routes["/v1/rerank"] = self.response({"results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.22}]})
        result = probe_model(self.model("rerank"))
        self.assertTrue(result["success"])
        self.assertEqual({"result_items": 2}, result["evidence"])
        body = json.loads(_Handler.requests[-1][1])
        self.assertEqual("synthetic", body["model"])
        self.assertEqual("排序验证", body["query"])
        self.assertEqual(2, body["top_n"])
        self.assertEqual(2, len(body["documents"]))
        _Handler.routes["/v1/rerank"] = self.response({"results": [{"index": 2, "relevance_score": math.nan}]})
        self.assertEqual("response_invalid", probe_model(self.model("rerank"))["code"])

    def test_speech_to_text_uses_fixture_and_checks_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "probe.wav"
            fixture.write_bytes(wav_bytes())
            _Handler.routes["/v1/audio/transcriptions"] = self.response({"text": "test"})
            with patch("codex_workbench.model_probe._PROBE_AUDIO", fixture):
                result = probe_model(self.model("speech_to_text"))
        self.assertTrue(result["success"])
        self.assertIn(b"RIFF", _Handler.requests[-1][1])

    def test_packaged_speech_fixture_can_drive_the_transcription_probe(self) -> None:
        """根目录提供的离线合成 WAV 必须能被真实 multipart 链路读取。"""
        _Handler.routes["/v1/audio/transcriptions"] = self.response({"text": "test"})
        self.assertTrue(probe_model(self.model("speech_to_text"))["success"])

    def test_unauthorized_redirect_invalid_and_oversize_are_safe(self) -> None:
        _Handler.routes["/v1/chat/completions"] = (401, {"Content-Type": "application/json"}, b'{"error":"secret-token"}')
        result = probe_model(self.model("reasoning"), token="secret-token")
        self.assertEqual("unauthorized", result["code"])
        self.assertNotIn("secret-token", repr(result))
        _Handler.routes["/v1/chat/completions"] = (302, {"Location": "https://other.invalid/x"}, b"")
        self.assertEqual("redirect_blocked", probe_model(self.model("reasoning"))["code"])
        self.assertEqual("validation_invalid_model", probe_model({**self.model("reasoning"), "base_url": self.base + "?key=secret-token"})["code"])
        _Handler.routes["/v1/chat/completions"] = (200, {"Content-Type": "application/json"}, b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual("response_oversize", probe_model(self.model("reasoning"))["code"])

    def test_empty_nan_json_audio_and_cancellation_fail(self) -> None:
        _Handler.routes["/v1/chat/completions"] = self.response({"choices": [{"message": {"content": "  "}}]})
        self.assertEqual("response_invalid", probe_model(self.model("reasoning"))["code"])
        _Handler.routes["/v1/chat/completions"] = self.response({
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 0}},
        })
        self.assertEqual("response_invalid", probe_model(self.model("reasoning"))["code"])
        _Handler.routes["/v1/embeddings"] = self.response({"data": [{"embedding": [math.nan]}]})
        self.assertEqual("response_invalid", probe_model(self.model("embedding"))["code"])
        _Handler.routes["/v1/embeddings"] = self.response({"data": [None]})
        self.assertEqual("response_invalid", probe_model(self.model("embedding"))["code"])
        _Handler.routes["/v1/audio/speech"] = self.response({"error": "not audio"})
        self.assertEqual("response_invalid", probe_model(self.model("text_to_speech"))["code"])
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual("cancelled", probe_model(self.model("reasoning"), cancel_event=cancelled)["code"])

    def test_missing_speech_fixture_has_stable_validation_result(self) -> None:
        with patch("codex_workbench.model_probe._PROBE_AUDIO", Path("/definitely/missing/probe.wav")):
            self.assertEqual("validation_fixture_missing", probe_model(self.model("speech_to_text"))["code"])

    def test_tts_rejects_tag_only_truncated_and_wrong_format_audio(self) -> None:
        for payload in (b"", b"ID3", b"ID3test-audio", b"ID3\x04\0\0\0\0\0\0", MP3[:-1], MP3[:4], wav_bytes()):
            with self.subTest(size=len(payload), prefix=payload[:10]):
                _Handler.routes["/v1/audio/speech"] = (200, {"Content-Type": "audio/mpeg"}, payload)
                result = probe_model(self.model("text_to_speech"))
                self.assertFalse(result["success"])
                self.assertEqual("response_invalid", result["code"])

    def test_invalid_input_types_are_validation_failures_not_exceptions(self) -> None:
        invalid_type = {**self.model("reasoning"), "model_type": []}
        self.assertEqual("validation_invalid_model", probe_model(invalid_type)["code"])
        invalid_url = {**self.model("reasoning"), "base_url": "https://[not-an-ip"}
        self.assertEqual("validation_invalid_model", probe_model(invalid_url)["code"])
        self.assertEqual("validation_invalid_model", probe_model(self.model("reasoning"), timeout=math.nan)["code"])


if __name__ == "__main__":
    unittest.main()
