"""写入前校验与格式兼容回归；仅使用内存合成值和临时保险库替身。"""

import base64
import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from codex_workbench.credential_payload import CredentialPayloadSessions, CredentialPayloadVault
from codex_workbench.credential_schema import CredentialSchemaError, normalize_payload, parse_payload, probe_structure, validate_structure
from codex_workbench.model_keys import KeyEnvelopeSessions, ModelKeyError
from codex_workbench.store import StoreError
from tests import test_credential_service as service_fixture
from tests import test_model_keys as encryption_fixture


CONTEXT = {"name": "合成凭证", "model_type": "credential", "base_url": "https://credential.invalid", "model_id": None, "version": None}


def envelope(sessions, raw):
    """使用测试公钥封装合成 UTF-8 文本。"""
    prepared = sessions.prepare(CONTEXT)
    return encryption_fixture.ModelKeysTest().envelope(prepared, raw)


def payload_json(value):
    """与浏览器 JSON.stringify 对齐，按紧凑 UTF-8 计算整包字节数。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CredentialWriteSafetyTest(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_invalid_or_valid_metadata_cannot_enable_removed_write(self):
        for metadata in [{},{'title':'合成配置'},{'title':'合成配置','tags':['示例']},{'password':'synthetic'}]:
            with self.subTest(metadata=list(metadata)),self.assertRaises(ValueError):self.board.call('credential_create',metadata)
        self.assertEqual([],self.fixture.secret_reads)
    def test_removed_payload_preparation_has_no_key_side_effect(self):
        with self.assertRaises(ValueError):self.board.call('credential_key_prepare',{'title':'合成配置'})
        self.assertEqual([],list(self.fixture.root.iterdir()))
    def test_no_organization_write_during_listing(self):
        self.board.call('credential_list',{});self.assertEqual([],list(self.fixture.root.iterdir()))
        with self.assertRaises(ValueError):self.board.call('credential_organization_apply',{})
    def test_plaintext_is_not_an_accepted_detail_parameter(self):
        with self.assertRaises(ValueError):self.board.call('credential_details',{'id':'x','public_key':'p','request_id':'r','payload':'synthetic-private'})
        self.assertEqual([],self.fixture.secret_reads)



class CredentialPayloadBoundaryTest(unittest.TestCase):
    """覆盖通用整包 UTF-8、AES-GCM 标签与独立模型 Key 契约。"""

    def test_exact_utf8_json_limit_and_one_byte_over(self):
        overhead = len(payload_json({"password": ""}).encode("utf-8"))
        payload = {"password": "中" * 2725 + "x" * (65536 - overhead - 2725 * 3)}
        raw = payload_json(payload)
        self.assertEqual(65536, len(raw.encode("utf-8")))
        self.assertEqual(payload, normalize_payload(payload))
        sessions = CredentialPayloadSessions()
        encrypted = envelope(sessions, raw)
        self.assertEqual(65552, len(base64.b64decode(encrypted["ciphertext"])))
        self.assertEqual(87404, len(encrypted["ciphertext"]))
        self.assertEqual(payload, sessions.consume_payload(encrypted, CONTEXT))
        too_large = {"password": payload["password"] + "x"}
        with self.assertRaises(CredentialSchemaError):
            normalize_payload(too_large)
        with self.assertRaises(ModelKeyError):
            sessions.consume_payload(envelope(sessions, payload_json(too_large)), CONTEXT)

    def test_total_multiple_fields_and_raw_whitespace_are_bounded(self):
        sessions = CredentialPayloadSessions()
        with self.assertRaises(CredentialSchemaError):
            normalize_payload({"password": "x" * 40000, "key": "y" * 40000})
        for raw in (payload_json({"password": "x" * 40000, "key": "y" * 40000}), '{}'+ ' ' * 65535):
            with self.assertRaises(ModelKeyError):
                sessions.consume_payload(envelope(sessions, raw), CONTEXT)

    def test_direct_vault_rejects_total_size_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = Path(temporary) / "unused-vault"
            command.touch()
            with patch("codex_workbench.credential_payload.subprocess.run") as run:
                with self.assertRaises(ModelKeyError):
                    CredentialPayloadVault(command).store({"password": "x" * 40000, "key": "y" * 40000}, CONTEXT)
                run.assert_not_called()

    def test_duplicate_json_keys_are_rejected_without_echo(self):
        for raw in ('{"password":"private-a","password":"private-b"}', '{"nested":{"x":1,"x":2}}', '{"port":NaN}'):
            with self.assertRaises(CredentialSchemaError) as caught:
                parse_payload(raw)
            self.assertNotIn("private", str(caught.exception))

    def test_model_key_keeps_independent_8192_ascii_contract(self):
        sessions = KeyEnvelopeSessions()
        key = "x" * 8192
        self.assertEqual(key, sessions.consume(envelope(sessions, key), CONTEXT))


class CredentialKnownFormatTest(unittest.TestCase):
    """仅从内存合成载荷识别格式，确保索引没有载荷值或任意名称。"""

    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.traditional = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-private-subject")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cls.certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                           .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
                           .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    def test_single_pem_blocks_have_only_fixed_structure(self):
        for raw, field, format_name in ((self.private, "private_key", "pem_private_key"),
                                        (self.traditional, "private_key", "pem_private_key"),
                                        (self.certificate, "certificate", "pem_certificate")):
            for content in (raw, b" \n" + raw + b"\t", raw.replace(b"\n", b"\r\n")):
                result = probe_structure(content)
                self.assertEqual(field, result["kind"])
                self.assertEqual(format_name, result["structure_format"])
                self.assertEqual({field: "string"}, result["field_types"])
                self.assertEqual(result, validate_structure(result))
                output = json.dumps(result)
                self.assertNotIn("synthetic-private-subject", output)
                self.assertNotIn(raw.splitlines()[1].decode(), output)

    def test_fake_mixed_and_multiple_pem_blocks_remain_unrecognized(self):
        for raw in (b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
                    self.private.replace(b"END PRIVATE KEY", b"END CERTIFICATE"),
                    self.private + self.private, self.private + self.certificate,
                    b"synthetic-private-prefix\n" + self.private,
                    self.certificate + b"synthetic-private-tail", b"ordinary-synthetic-text"):
            result = probe_structure(raw)
            self.assertEqual("unrecognized", result["structure_status"])
            self.assertEqual("unknown", result["kind"])
            self.assertEqual({}, result["field_types"])
            self.assertEqual(result, validate_structure(result))
            self.assertNotIn("synthetic-private", json.dumps(result))

    def test_legacy_types_are_preserved_and_new_payload_stays_strict(self):
        for port, expected_type in ((443, "integer"), (443.0, "number"), ("443", "string")):
            fields = {"account": None, "port": port, "password": "synthetic-password"}
            for value in (fields, {"credential": fields, "target": {}}):
                raw = payload_json(value).encode()
                result = probe_structure(raw)
                self.assertEqual("indexed", result["structure_status"])
                self.assertEqual({"account": "null", "port": expected_type, "password": "string"}, result["field_types"])
                self.assertEqual(result, validate_structure(result))
                self.assertEqual(raw, payload_json(value).encode())
            with self.assertRaises(CredentialSchemaError):
                normalize_payload(fields)
        for fields in ({"password": {"arbitrary-secret-name": "synthetic-value"}}, {"port": True}, {"account": False}):
            result = probe_structure(payload_json(fields).encode())
            self.assertEqual("invalid_field_type", result["structure_error"])
            self.assertEqual(result, validate_structure(result))
            self.assertNotIn("arbitrary-secret-name", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
