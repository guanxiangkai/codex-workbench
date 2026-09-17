"""不监听端口的媒体集成回归；真实 HTTP 由既有回环测试负责。"""

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.capability_manifest import create_capability_manifest, load_active_manifest, listed_capability_artifact, read_capability_events
from codex_workbench.capability_runtime import CapabilityRuntime
from codex_workbench.media_validation import valid_media
from codex_workbench.model_probe import probe_model
from media_fixtures import PNG, MP3, wav_bytes


class MediaContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.image = self.root / "image.png"
        self.image.write_bytes(PNG)
        self.audio = self.root / "audio.wav"
        self.audio.write_bytes(wav_bytes())
        self.models = [dict(id=kind, model_type=kind, base_url="https://synthetic.invalid/v1", model="synthetic",
                            protocol="openai-chat", voice="alloy", credential_ref="", validation_status="verified")
                       for kind in ("multimodal", "speech_to_text", "text_to_speech")]
        resources = [dict(snapshotPath=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()) for path in (self.image, self.audio)]
        self.binding = create_capability_manifest(self.root, "00000000-0000-0000-0000-000000000001", str(self.workspace), "workspace-write", self.models, resources)
        self.manifest = load_active_manifest(self.binding.manifest_path)
        self.runtime = CapabilityRuntime(self.binding.manifest_path)

    def test_probe_tts_uses_mp3_structure_and_preserves_result_contract(self):
        for payload, expected in ((MP3, True), (b"ID3", False), (MP3[:-1], False), (wav_bytes(), False)):
            with self.subTest(size=len(payload)), patch("codex_workbench.model_probe._post", return_value=(payload, "audio/mpeg", None)) as post:
                result = probe_model(self.models[2])
                self.assertEqual(expected, result["success"])
                self.assertEqual("verified" if expected else "response_invalid", result["code"])
                self.assertEqual("mp3", json.loads(post.call_args.args[1])["response_format"])
                if expected:
                    self.assertEqual({"audio_bytes": len(MP3)}, result["evidence"])

    def test_selected_bytes_reaching_consumer_match_snapshot_digest(self):
        for tool, model, key, path in (("multimodal_image", "multimodal", "image", self.image),
                                      ("speech_to_text", "speech_to_text", "audio", self.audio)):
            with self.subTest(tool=tool), patch.object(self.runtime, "_invoke", return_value={"success": True, "text": "ok"}) as invoke:
                result = self.runtime.call(tool, {"model_id": model, key + "_path": str(path), **({"prompt": "describe"} if key == "image" else {})})
                self.assertNotIn("isError", result)
                sent = invoke.call_args.args[2][key + "_bytes"]
                selected = next(item for item in self.manifest["resource_snapshots"] if item["path"] == str(path.resolve()))
                self.assertEqual(selected["sha256"], hashlib.sha256(sent).hexdigest())
                self.assertEqual(path.read_bytes(), sent)

    def test_valid_replacement_after_path_check_is_rejected_for_snapshot(self):
        original = self.runtime._input_path

        def replaced(source, manifest):
            path = original(source, manifest)
            replacement = wav_bytes(b"\x01\0" * 32)
            self.assertTrue(valid_media(replacement, ".wav", "audio"))
            path.write_bytes(replacement)
            return path

        with patch.object(self.runtime, "_input_path", side_effect=replaced), patch.object(self.runtime, "_invoke") as invoke:
            result = self.runtime.call("speech_to_text", {"model_id": "speech_to_text", "audio_path": str(self.audio)})
            self.assertTrue(result["isError"])
            invoke.assert_not_called()

    def test_tts_artifact_roundtrip_and_changed_actual_bytes_are_rejected(self):
        with patch.object(self.runtime, "_invoke", return_value={"success": True, "audio": base64.b64encode(MP3).decode()}):
            generated = self.runtime.call("text_to_speech", {"model_id": "text_to_speech", "input": "hello"})
        self.assertNotIn("isError", generated)
        path = Path(generated["artifact_path"])
        self.assertEqual(MP3, path.read_bytes())
        self.assertTrue(listed_capability_artifact(self.manifest, path, content_sha256=hashlib.sha256(MP3).hexdigest()))
        with patch.object(self.runtime, "_invoke", return_value={"success": True, "text": "hello"}) as invoke:
            self.assertNotIn("isError", self.runtime.call("speech_to_text", {"model_id": "speech_to_text", "audio_path": str(path)}))
            self.assertEqual(MP3, invoke.call_args.args[2]["audio_bytes"])
        original = self.runtime._input_path

        def replaced(source, manifest):
            checked = original(source, manifest)
            replacement = MP3 + MP3[:417]
            self.assertTrue(valid_media(replacement, ".mp3", "audio"))
            checked.write_bytes(replacement)
            return checked

        with patch.object(self.runtime, "_input_path", side_effect=replaced), patch.object(self.runtime, "_invoke") as invoke:
            self.assertTrue(self.runtime.call("speech_to_text", {"model_id": "speech_to_text", "audio_path": str(path)})["isError"])
            invoke.assert_not_called()

    def test_invalid_tts_does_not_write_or_register_audio(self):
        for payload in (b"ID3", MP3[:-1], wav_bytes()):
            with self.subTest(size=len(payload)), patch.object(self.runtime, "_invoke", return_value={"success": True, "audio": base64.b64encode(payload).decode()}):
                result = self.runtime.call("text_to_speech", {"model_id": "text_to_speech", "input": "hello"})
                self.assertTrue(result["isError"])
                self.assertIn("response_invalid", result["content"][0]["text"])
                self.assertEqual([], list(Path(self.manifest["artifacts_dir"]).iterdir()))
                ledger = Path(self.manifest["artifacts_path"])
                self.assertFalse(ledger.exists() and ledger.read_bytes())
        self.assertTrue(all(not item["success"] for item in read_capability_events(self.binding.manifest_path)))

    def test_isolated_stdio_entry_can_import_validator_and_list_tools(self):
        entry = Path(__file__).resolve().parents[1] / "src/codex_workbench/capability_runtime.py"
        messages = [dict(jsonrpc="2.0", id=1, method="initialize", params={"protocolVersion": "2024-11-05"}),
                    dict(jsonrpc="2.0", id=2, method="tools/list")]
        result = subprocess.run([sys.executable, "-I", "-B", str(entry), self.binding.manifest_path],
                                input="".join(json.dumps(item) + "\n" for item in messages), text=True,
                                capture_output=True, timeout=5, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([1, 2], [item["id"] for item in responses])
        self.assertEqual(3, len(responses[1]["result"]["tools"]))


if __name__ == "__main__":
    unittest.main()
