"""模型密钥信封与保险库写入仅使用合成值和临时假脚本。"""

from __future__ import annotations

import base64
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codex_workbench.model_keys import KeyEnvelopeSessions, ModelKeyError, ModelKeyVault


class ModelKeysTest(unittest.TestCase):
    """加密解封与受控 stdin 写入的契约测试。"""

    def setUp(self) -> None:
        self.context = {
            "name": "合成推理模型", "model_type": "reasoning", "base_url": "http://127.0.0.1:9999/v1",
            "model_id": None, "version": None,
        }
        self.sessions = KeyEnvelopeSessions()

    def envelope(self, prepared: dict, key: str = "synthetic-key-123") -> dict:
        public_key = serialization.load_der_public_key(base64.b64decode(prepared["public_key"], validate=True))
        aes_key, iv = os.urandom(32), os.urandom(12)
        ciphertext = AESGCM(aes_key).encrypt(iv, key.encode("utf-8"), prepared["id"].encode("utf-8"))
        wrapped = public_key.encrypt(
            aes_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        return {
            "session_id": prepared["id"], "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
            "iv": base64.b64encode(iv).decode("ascii"), "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def assert_error(self, code: str, callable, *args) -> None:
        with self.assertRaises(ModelKeyError) as caught:
            callable(*args)
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn("synthetic-key-123", str(caught.exception))

    def test_prepare_returns_spki_and_consume_returns_key_once(self) -> None:
        with patch("codex_workbench.model_keys.time.time", return_value=100):
            prepared = self.sessions.prepare(self.context)
        public = serialization.load_der_public_key(base64.b64decode(prepared["public_key"], validate=True))
        self.assertEqual(2048, public.key_size)
        self.assertEqual(400, prepared["expires_at"])
        envelope = self.envelope(prepared)
        with patch("codex_workbench.model_keys.time.time", return_value=101):
            self.assertEqual("synthetic-key-123", self.sessions.consume(envelope, self.context))
            self.assert_error("session_invalid", self.sessions.consume, envelope, self.context)

    def test_tamper_context_change_and_invalid_key_are_rejected_without_reuse(self) -> None:
        prepared = self.sessions.prepare(self.context)
        tampered = self.envelope(prepared)
        tampered["ciphertext"] = tampered["ciphertext"][:-2] + "AA"
        self.assert_error("envelope_invalid", self.sessions.consume, tampered, self.context)
        self.assert_error("session_invalid", self.sessions.consume, tampered, self.context)

        prepared = self.sessions.prepare(self.context)
        envelope = self.envelope(prepared)
        changed = {**self.context, "base_url": "http://127.0.0.1:10000/v1"}
        self.assert_error("context_mismatch", self.sessions.consume, envelope, changed)
        self.assert_error("session_invalid", self.sessions.consume, envelope, self.context)

        prepared = self.sessions.prepare(self.context)
        self.assert_error("key_invalid", self.sessions.consume, self.envelope(prepared, "bad\nkey"), self.context)

    def test_expiry_limits_and_context_shape_are_enforced(self) -> None:
        with patch("codex_workbench.model_keys.time.time", return_value=100):
            prepared = self.sessions.prepare(self.context)
            envelope = self.envelope(prepared)
        with patch("codex_workbench.model_keys.time.time", return_value=401):
            self.assert_error("session_invalid", self.sessions.consume, envelope, self.context)
        self.assert_error("context_invalid", self.sessions.prepare, {**self.context, "extra": "forbidden"})
        limited = KeyEnvelopeSessions()
        for _ in range(32):
            limited.prepare(self.context)
        self.assert_error("session_limit", limited.prepare, self.context)

    def test_fake_vault_receives_only_stdin_payload_then_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured, arguments, script = root / "payload.json", root / "arguments.txt", root / "vault.sh"
            script.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments))}\n"
                "if [ \"$1\" = put-stdin ]; then cat > " + shlex.quote(str(captured)) + "; exit 0; fi\n"
                "if [ \"$1\" = verify ]; then [ \"${FAKE_VERIFY_FAIL:-0}\" = 0 ] || exit 7; exit 0; fi\n"
                "exit 64\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            reference = ModelKeyVault(script).store("synthetic-key-123", self.context)
            self.assertRegex(reference, r"^vault:model-[A-Za-z0-9-]+$")
            payload = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual("synthetic-key-123", payload["api_key"])
            self.assertEqual(self.context, payload["target"])
            self.assertNotIn("synthetic-key-123", arguments.read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"FAKE_VERIFY_FAIL": "1"}, clear=False):
                self.assert_error("vault_verify_failed", ModelKeyVault(script).store, "synthetic-key-123", self.context)


if __name__ == "__main__":
    unittest.main()
