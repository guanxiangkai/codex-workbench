import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.model_probe import _PNG, _provider_error_diagnostic
from codex_workbench.registered_model_verifier import (_downloadable_https_url, _image_output, _minimax_image_output,
                                                       _video_result, configuration_fingerprint, verify_model,
                                                       verify_model_with_review, write_metadata, write_reviews)


def _verification_script():
    """加载 CLI 的纯本地凭据边界，测试时不执行其 main。"""
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_registered_models.py"
    spec = importlib.util.spec_from_file_location("verify_registered_models_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RegisteredModelVerifierTests(unittest.TestCase):
    def model(self, **changes):
        value = {
            "id": "glm", "provider_id": "bigmodel", "model_type": "reasoning", "model": "glm-5.3",
            "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        }
        return {**value, **changes}

    def test_only_registered_https_host_can_receive_a_credential(self):
        for url in ("http://open.bigmodel.cn/api/paas/v4/chat/completions", "https://elsewhere.invalid/v1"):
            with patch("codex_workbench.registered_model_verifier._native_probe") as probe:
                result = verify_model(self.model(base_url=url), "synthetic-key")
            self.assertEqual({"model_id": "glm", "success": False, "status": "skipped", "error_code": "unregistered_https_endpoint"}, result)
            probe.assert_not_called()

    def test_native_ocr_and_asr_send_documented_generated_fixture_payloads(self):
        ocr = self.model(model_type="multimodal", base_url="https://open.bigmodel.cn/api/paas/v4/layout_parsing")
        with patch("codex_workbench.registered_model_verifier._post", return_value=(b'{"md_results":"# OK"}', "application/json", None)) as post:
            self.assertTrue(verify_model(ocr, "synthetic-key")["success"])
        body = json.loads(post.call_args.args[1])
        self.assertEqual("glm-5.3", body["model"])
        self.assertIsInstance(body["file"], str)
        self.assertTrue(body["file"].startswith("data:image/png;base64,"))

        asr = self.model(provider_id="minimax", model_type="speech_to_text", model="asr-1.0",
                         base_url="https://api.minimax.cn/v1/speech_to_text")
        with patch("codex_workbench.registered_model_verifier.Path.read_bytes", return_value=b"RIFFfixture"), \
             patch("codex_workbench.registered_model_verifier._post", return_value=(b'{"text":"OK"}', "application/json", None)) as post:
            self.assertTrue(verify_model(asr, "synthetic-key")["success"])
        self.assertIn(b'name="model"', post.call_args.args[1])
        self.assertIn(b'asr-1.0', post.call_args.args[1])
        self.assertIn("multipart/form-data", post.call_args.args[2])

    def test_minimax_native_tts_and_image_follow_official_shapes(self):
        tts = self.model(provider_id="minimax", model_type="text_to_speech", model="speech-2.8-hd",
                         base_url="https://api.minimax.cn/v1/t2a_v2")
        audio = b"not-real-mp3"
        response = json.dumps({"base_resp": {"status_code": 0}, "data": {"audio": base64.b64encode(audio).decode()}}).encode()
        with patch("codex_workbench.registered_model_verifier._audio_bytes", return_value=True), \
             patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)) as post:
            self.assertTrue(verify_model(tts, "synthetic-key")["success"])
        body = json.loads(post.call_args.args[1])
        self.assertEqual("English_Graceful_Lady", body["voice_setting"]["voice_id"])
        self.assertEqual("mp3", body["audio_setting"]["format"])

        image = self.model(provider_id="minimax", model_type="image_generation", model="image-01",
                           base_url="https://api.minimax.cn/v1/image_generation")
        response = json.dumps({"base_resp": {"status_code": 0}, "data": {"image_base64": [_PNG]}}).encode()
        with patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)) as post:
            self.assertTrue(verify_model(image, "synthetic-key")["success"])
        body = json.loads(post.call_args.args[1])
        self.assertEqual("1:1", body["aspect_ratio"])
        self.assertEqual("base64", body["response_format"])

    def test_minimax_malformed_base_response_fails_without_aborting(self):
        image = self.model(provider_id="minimax", model_type="image_generation", model="image-01",
                           base_url="https://api.minimax.cn/v1/image_generation")
        tts = self.model(provider_id="minimax", model_type="text_to_speech", model="speech-2.8-hd",
                         base_url="https://api.minimax.cn/v1/t2a_v2")
        for model, response in ((image, b'{"base_resp":null,"data":{}}'),
                                (tts, b'{"base_resp":"failed","data":{"audio":"52494646"}}')):
            with self.subTest(model=model["id"]), \
                 patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)):
                self.assertFalse(verify_model(model, "synthetic-key")["success"])

    def test_image_base64_requires_a_supported_structural_container(self):
        junk = base64.b64encode(b"this is long enough but is not an image").decode()
        cases = (
            (self.model(model_type="image_generation", model="image-01",
                        base_url="https://open.bigmodel.cn/api/paas/v4/images/generations"),
             {"data": [{"b64_json": junk}]}),
            (self.model(provider_id="minimax", model_type="image_generation", model="image-01",
                        base_url="https://api.minimax.cn/v1/image_generation"),
             {"base_resp": {"status_code": 0}, "data": {"image_base64": [junk]}}),
        )
        for image, response in cases:
            with self.subTest(provider=image["provider_id"]), \
                 patch("codex_workbench.registered_model_verifier._post", return_value=(json.dumps(response).encode(), "application/json", None)):
                self.assertFalse(verify_model(image, "synthetic-key")["success"])

    def test_minimax_tts_tries_hex_audio_before_base64(self):
        tts = self.model(provider_id="minimax", model_type="text_to_speech", model="speech-2.8-hd",
                         base_url="https://api.minimax.cn/v1/t2a_v2")
        response = b'{"base_resp":{"status_code":0},"data":{"audio":"52494646"}}'
        with patch("codex_workbench.registered_model_verifier._audio_bytes", side_effect=lambda value: value == b"RIFF"), \
             patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)):
            self.assertTrue(verify_model(tts, "synthetic-key")["success"])

    def test_bigmodel_tts_uses_documented_voice_and_wav_response(self):
        tts = self.model(model_type="text_to_speech", model="glm-tts",
                         base_url="https://open.bigmodel.cn/api/paas/v4/audio/speech")
        with patch("codex_workbench.registered_model_verifier.valid_media", return_value=True), \
             patch("codex_workbench.registered_model_verifier._post", return_value=(b"RIFFfixture", "audio/wav", None)) as post:
            self.assertTrue(verify_model(tts, "synthetic-key")["success"])
        body = json.loads(post.call_args.args[1])
        self.assertEqual({"model": "glm-tts", "input": "模型能力验证。", "voice": "tongtong", "response_format": "wav"}, body)
        self.assertEqual("audio/wav, application/json", post.call_args.kwargs["accept"])

    def test_video_task_acceptance_never_records_success(self):
        model = self.model(id="video", model_type="video_generation", model="cogvideox-3",
                           base_url="https://open.bigmodel.cn/api/paas/v4/videos/generations")
        accepted = json.dumps({"task_id": "task-1", "status": "processing"}).encode()
        with patch("codex_workbench.registered_model_verifier._post", return_value=(accepted, "application/json", None)), \
             patch("codex_workbench.registered_model_verifier._get", return_value=(accepted, None)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            result = verify_model(model, "synthetic-key", timeout=2)
        self.assertEqual({"model_id": "video", "success": False, "status": "failed", "error_code": "task_pending"}, result)

    def test_video_requires_terminal_result_url(self):
        model = self.model(id="video", model_type="video_generation", model="cogvideox-3",
                           base_url="https://open.bigmodel.cn/api/paas/v4/videos/generations")
        accepted = json.dumps({"task_id": "task-1", "status": "processing"}).encode()
        completed = json.dumps({"task_id": "task-1", "status": "success", "video_result": {"url": "https://cdn.example/video.mp4"}}).encode()
        with patch("codex_workbench.registered_model_verifier._post", return_value=(accepted, "application/json", None)), \
             patch("codex_workbench.registered_model_verifier._get", return_value=(completed, None)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            result = verify_model(model, "synthetic-key", timeout=2)
        self.assertEqual({"model_id": "video", "success": True, "status": "verified", "error_code": "verified"}, result)

    def test_video_requires_a_valid_downloadable_https_url(self):
        for url in ("", "http://cdn.example/video.mp4", "https://", "https://user@cdn.example/video.mp4", "https://cdn.example/video.mp4 bad"):
            with self.subTest(url=url):
                task_id, verified = _video_result({"task_id": "task-1", "status": "success", "video_result": {"url": url}})
                self.assertEqual("task-1", task_id)
                self.assertFalse(verified)
        _, verified = _video_result({"task_id": "task-1", "status": "success", "video_result": {"url": "https://cdn.example/video.mp4?signature=abc"}})
        self.assertTrue(verified)

    def test_image_urls_require_a_valid_downloadable_https_url(self):
        for url in ("https://", "https://user@cdn.example/image.png", " https://cdn.example/image.png",
                    "https://cdn.example/image.png bad"):
            with self.subTest(url=url):
                self.assertFalse(_downloadable_https_url(url))
        for checker, response in ((_image_output, {"data": [{"url": "https://user@cdn.example/image.png"}]}),
                                  (_minimax_image_output, {"base_resp": {"status_code": 0},
                                                           "data": {"image_urls": ["https://"]}})):
            self.assertFalse(checker(response))

    def test_structured_target_must_match_every_selected_endpoint(self):
        script = _verification_script()
        selected = [{"base_url": "https://api.minimax.cn/v1/image_generation"},
                    {"base_url": "https://api.minimax.cn/v1/t2a_v2"}]
        credential = json.dumps({"api_key": "synthetic-key", "target":
                                 {"base_url": selected[0]["base_url"]}}).encode()
        with self.assertRaisesRegex(ValueError, "credential_target"):
            script._decode_selected_credential(credential, selected)

    def test_minimax_video_requires_polled_terminal_content_url(self):
        model = self.model(id="video", provider_id="minimax", model_type="video_generation", model="MiniMax-H3",
                           base_url="https://api.minimax.cn/v2/video_generation")
        accepted = b'{"task_id":"task-1"}'
        completed = b'{"task":{"id":"task-1","status":"succeeded","content":{"url":"https://cdn.example/video.mp4"}}}'
        with patch("codex_workbench.registered_model_verifier._post", return_value=(accepted, "application/json", None)) as post, \
             patch("codex_workbench.registered_model_verifier._get", return_value=(completed, None)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            self.assertTrue(verify_model(model, "synthetic-key", timeout=2)["success"])
        body = json.loads(post.call_args.args[1])
        self.assertEqual([{"type": "text", "text": body["content"][0]["text"]}], body["content"])
        self.assertEqual(4, body["duration"])

    def test_pending_video_task_is_resumed_without_a_second_submission(self):
        model = self.model(id="video", provider_id="minimax", model_type="video_generation", model="MiniMax-H3",
                           base_url="https://api.minimax.cn/v2/video_generation", _verification_task_id="task-1")
        completed = b'{"task":{"id":"task-1","status":"succeeded","content":{"url":"https://cdn.example/video.mp4"}}}'
        with patch("codex_workbench.registered_model_verifier._post") as post, \
             patch("codex_workbench.registered_model_verifier._get", return_value=(completed, None)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            self.assertTrue(verify_model(model, "synthetic-key", timeout=2)["success"])
        post.assert_not_called()

    def test_video_poll_failure_keeps_task_id_for_a_safe_resume(self):
        model = self.model(id="video", provider_id="minimax", model_type="video_generation", model="MiniMax-H3",
                           base_url="https://api.minimax.cn/v2/video_generation")
        accepted = b'{"task_id":"task-1"}'
        failure = {"success": False, "code": "network_error"}
        with patch("codex_workbench.registered_model_verifier._post", return_value=(accepted, "application/json", None)), \
             patch("codex_workbench.registered_model_verifier._get", return_value=(None, failure)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            safe, _, diagnostic = verify_model_with_review(model, "synthetic-key", timeout=2, include_diagnostics=True)
        self.assertEqual("network_error", safe["error_code"])
        self.assertEqual({"video_task_id": "task-1"}, diagnostic)

    def test_video_terminal_failure_is_not_persisted_for_resume(self):
        model = self.model(id="video", provider_id="minimax", model_type="video_generation", model="MiniMax-H3",
                           base_url="https://api.minimax.cn/v2/video_generation")
        accepted = b'{"task_id":"task-1"}'
        failed = b'{"task":{"id":"task-1","status":"failed"}}'
        with patch("codex_workbench.registered_model_verifier._post", return_value=(accepted, "application/json", None)), \
             patch("codex_workbench.registered_model_verifier._get", return_value=(failed, None)), \
             patch("codex_workbench.registered_model_verifier.time.sleep"):
            safe, _, diagnostic = verify_model_with_review(model, "synthetic-key", timeout=2, include_diagnostics=True)
        self.assertEqual("task_failed", safe["error_code"])
        self.assertEqual({}, diagnostic)

    def test_configuration_fingerprint_binds_resume_to_model_endpoint_and_credential_identity(self):
        model = self.model(id="video", model_type="video_generation", model="MiniMax-H3",
                           provider_id="minimax", credential_id="credential-a",
                           base_url="https://api.minimax.cn/v2/video_generation")
        fingerprint = configuration_fingerprint(model, "token-a")
        self.assertEqual(fingerprint, configuration_fingerprint(dict(model), "token-a"))
        for change in ({"base_url": "https://api.minimax.cn/v2/video_generation_alt"},
                       {"model": "MiniMax-H3-alt"}, {"credential_id": "credential-b"}):
            self.assertNotEqual(fingerprint, configuration_fingerprint({**model, **change}, "token-a"))
        self.assertNotEqual(fingerprint, configuration_fingerprint(model, "token-b"))
        self.assertNotIn("credential-a", fingerprint)
        self.assertNotIn("token-a", fingerprint)

    def test_structured_credential_target_must_match_a_selected_endpoint(self):
        script = _verification_script()
        selected = [{"base_url": "https://api.minimax.cn/v1/image_generation"}]
        credential = json.dumps({"api_key": "synthetic-key", "target": {"base_url": selected[0]["base_url"]}}).encode()
        self.assertEqual("synthetic-key", script._decode_selected_credential(credential, selected))
        mismatched = json.dumps({"api_key": "synthetic-key", "target": {"base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions"}}).encode()
        with self.assertRaisesRegex(ValueError, "credential_target"):
            script._decode_selected_credential(mismatched, selected)
        self.assertEqual("synthetic-key", script._decode_selected_credential(b"synthetic-key", selected))

    def test_metadata_has_no_response_or_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            write_metadata(Path(directory), [{"model_id": "glm", "success": True, "status": "verified", "error_code": "verified"}])
            write_metadata(Path(directory), [{"model_id": "minimax", "success": False, "status": "skipped", "error_code": "official_contract_unavailable"}])
            value = (Path(directory) / "registered-model-verifications.json").read_text()
        self.assertIn("account_profile_freeze", value)
        self.assertIn("glm", value)
        self.assertIn("minimax", value)
        self.assertNotIn("synthetic-key", value)
        self.assertNotIn("response", value)

    def test_reviews_are_separate_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            write_reviews(Path(directory), {"glm": "1. boundary"})
            target = Path(directory) / "registered-model-reviews.json"
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            value = json.loads(target.read_text())
        self.assertEqual("1. boundary", value["reviews"][0]["review"])

    def test_chat_review_is_not_part_of_safe_result(self):
        response = b'{"choices":[{"message":{"content":"1. first\\n2. usage\\n3. identity"}}]}'
        with patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)):
            result, review = verify_model_with_review(self.model(), "synthetic-key")
        self.assertEqual({"model_id": "glm", "success": True, "status": "verified", "error_code": "verified"}, result)
        self.assertEqual("1. first\n2. usage\n3. identity", review)

    def test_bigmodel_review_uses_supported_reasoning_effort_and_requested_timeout(self):
        response = b'{"choices":[{"message":{"content":"1. first"}}]}'
        with patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)) as post:
            verify_model_with_review(self.model(), "synthetic-key", timeout=45)
        request = json.loads(post.call_args.args[1])
        self.assertNotIn("thinking", request)
        self.assertEqual("low", request["reasoning_effort"])
        self.assertEqual(2048, request["max_completion_tokens"])
        self.assertEqual(45, post.call_args.kwargs["response_timeout"])

    def test_provider_diagnostics_classify_official_codes_without_messages(self):
        quota = _provider_error_diagnostic(b'{"base_resp":{"status_code":1008,"status_msg":"secret"}}', 200)
        parameter = _provider_error_diagnostic(b'{"error":{"code":2013,"message":"request details"}}', 400)
        unsupported = _provider_error_diagnostic(b'{"error":{"message":"not found"}}', 404)
        payment = _provider_error_diagnostic(b'{"error":{"message":"billing details"}}', 402)
        self.assertEqual({"diagnosis": "insufficient_balance", "http_status": 200, "provider_error_code": "1008"}, quota)
        self.assertEqual({"diagnosis": "parameter_error", "http_status": 400, "provider_error_code": "2013"}, parameter)
        self.assertEqual({"diagnosis": "endpoint_or_model_unsupported", "http_status": 404}, unsupported)
        self.assertEqual({"diagnosis": "payment_required", "http_status": 402}, payment)
        self.assertNotIn("secret", json.dumps(quota))

    def test_in_band_provider_error_is_available_only_to_private_metadata_path(self):
        model = self.model(provider_id="minimax", model_type="image_generation", model="image-01",
                           base_url="https://api.minimax.cn/v1/image_generation")
        response = b'{"base_resp":{"status_code":1008,"status_msg":"balance details"}}'
        with patch("codex_workbench.registered_model_verifier._post", return_value=(response, "application/json", None)):
            safe, review, diagnostic = verify_model_with_review(model, "synthetic-key", include_diagnostics=True)
        self.assertIsNone(review)
        self.assertEqual({"model_id": "glm", "success": False, "status": "failed", "error_code": "insufficient_balance"}, safe)
        self.assertEqual({"diagnosis": "insufficient_balance", "http_status": 200, "provider_error_code": "1008"}, diagnostic)


if __name__ == "__main__":
    unittest.main()
