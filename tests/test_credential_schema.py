"""结构探测只使用合成载荷；原值与任意键名不进入结果或消费者输出。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from codex_workbench.credential_schema import (
    CredentialSchemaError, MAX_PROBE_BYTES, SCHEMA_NAME, build_model_key_index,
    build_payload_index, normalize_payload, probe_structure, validate_structure,
)


class CredentialSchemaTest(unittest.TestCase):
    """覆盖新建、旧格式、部分识别、无结构、错误边界和 stdout 契约。"""

    def probe(self, value):
        return probe_structure(json.dumps(value).encode())

    def test_new_create_and_probe_produce_identical_structure(self):
        for fields, kind in (({}, "credential"), ({"description": "合成"}, "credential"),
                             ({"account": "synthetic-user", "password": "synthetic-password"}, "password"),
                             ({"key": "synthetic-key", "endpoint": "https://synthetic.invalid"}, "api_key"),
                             ({"key": "synthetic-key", "password": "synthetic-password", "port": 443}, "credential")):
            with self.subTest(kind=kind, fields=list(fields)):
                index = build_payload_index(fields)
                self.assertEqual(kind, index["kind"])
                self.assertEqual(index, self.probe({"schema": SCHEMA_NAME, "version": 1, "credential": fields, "target": {}}))
                self.assertEqual(index, validate_structure(index))
        self.assertEqual({}, normalize_payload({"password": ""}))
        for invalid in ({"unknown": "synthetic"}, {"port": True}, {"port": 0}, {"port": 65536}, {"password": 3}):
            with self.assertRaises(CredentialSchemaError):
                build_payload_index(invalid)

    def test_extended_create_fields_keep_original_string_types_and_port_integer(self):
        payload = {"description": "synthetic-description", "account": "synthetic-account", "password": "synthetic-password",
                   "ip": "192.0.2.1", "remote_path": "/synthetic", "port": 443, "endpoint": "https://synthetic.invalid",
                   "key": "synthetic-key", "host": "synthetic-host", "token": "synthetic-token",
                   "private_key": "synthetic-private", "public_key": "synthetic-public", "passphrase": "synthetic-passphrase",
                   "certificate": "synthetic-certificate"}
        self.assertEqual(payload, normalize_payload(payload))
        self.assertEqual(sorted(payload), build_payload_index(payload)["has_fields"])
        for field in set(payload) - {"port"}:
            invalid = dict(payload)
            invalid[field] = 1
            with self.assertRaises(CredentialSchemaError):
                normalize_payload(invalid)

    def test_legacy_payload_formats_have_fixed_canonical_fields(self):
        legacy = self.probe({"credential": {"account": "synthetic-user", "password": "synthetic-password"}, "target": {"name": "ssh.api.fake"}})
        self.assertEqual("workbench_credential", legacy["structure_format"])
        self.assertEqual("password", legacy["kind"])
        self.assertEqual(["account", "password"], legacy["has_fields"])
        model = self.probe({"api_key": "synthetic-key", "target": {"base_url": "https://synthetic.invalid", "name": "private"}})
        self.assertEqual(build_model_key_index(), model)
        private = self.probe({"private_key": "synthetic-private-material"})
        self.assertEqual("private_key", private["kind"])
        self.assertNotEqual("ssh", private["kind"])
        self.assertEqual("certificate", self.probe({"certificate": "synthetic-cert"})["kind"])

    def test_unknown_keys_and_nonstructured_values_never_leak(self):
        marker = "synthetic-private-account-and-key-name"
        cases = ({marker: marker}, {"password": marker, marker: marker}, {"nested": {"password": marker}}, [marker], marker)
        for value in cases:
            index = self.probe(value)
            self.assertNotIn(marker, json.dumps(index))
            validate_structure(index)
        partial = self.probe({"password": marker, marker: marker})
        self.assertTrue(partial["has_unknown_fields"])
        self.assertEqual(["password"], partial["has_fields"])
        unknown = self.probe({"title": "SSH Password API Key"})
        self.assertEqual("unknown", unknown["kind"])
        self.assertEqual("unrecognized", unknown["structure_status"])
        self.assertEqual("unrecognized", self.probe({"credential": {marker: marker}})["structure_status"])
        self.assertEqual("text", probe_structure(b"synthetic-private-value")["structure_format"])

    def test_malformed_limits_and_duplicate_keys_return_fixed_errors(self):
        for raw, code in ((b"\xff", "invalid_utf8"), (b"{broken-secret", "invalid_json"),
                          (b'{"password":"a","password":"secret"}', "invalid_json"),
                          (b'{"password":NaN}', "invalid_json"),
                          (b"a" * (MAX_PROBE_BYTES + 1), "payload_too_large")):
            result = probe_structure(raw)
            self.assertEqual(code, result["structure_error"])
            self.assertEqual("unknown", result["kind"])
            validate_structure(result)
        deep = {}; current = deep
        for _ in range(33):
            current["nested"] = {}; current = current["nested"]
        self.assertEqual("structure_limit", self.probe(deep)["structure_error"])
        self.assertEqual("invalid_schema", self.probe({"schema": SCHEMA_NAME, "version": 2, "credential": {}})["structure_error"])
        wrong = self.probe({"password": {"secret-key": "secret-value"}})
        self.assertEqual("invalid_field_type", wrong["structure_error"])
        self.assertEqual({"password": "object"}, wrong["field_types"])
        self.assertEqual("duplicate_field", self.probe({"account": "a", "username": "b"})["structure_error"])

    def test_stdin_consumer_outputs_only_index_and_never_stderr(self):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        marker = "synthetic-private-key-and-name"
        process = subprocess.run([sys.executable, "-m", "codex_workbench.credential_probe"],
                                 input=json.dumps({"password": marker, marker: marker}).encode(),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=5)
        self.assertEqual(0, process.returncode)
        self.assertEqual(b"", process.stderr)
        self.assertNotIn(marker.encode(), process.stdout)
        self.assertEqual(["password"], json.loads(process.stdout)["has_fields"])

    def test_index_ingestion_rejects_extra_fields_and_forged_types(self):
        for mutate in (lambda item: item.update(raw="synthetic-secret"),
                       lambda item: item["field_types"].update(secret_name="string"),
                       lambda item: item.update(kind="ssh"),
                       lambda item: item.update(structure_format="text"),
                       lambda item: item.update(structure_format="workbench_model_key"),
                       lambda item: item.update(structure_error="synthetic-secret")):
            index = build_payload_index({})
            mutate(index)
            with self.assertRaises(CredentialSchemaError) as caught:
                validate_structure(index)
            self.assertEqual("凭证结构索引无效", str(caught.exception))
