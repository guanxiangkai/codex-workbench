"""格式状态与逐对象摘要只使用合成载荷，不读取真实保险库或既有摘要。"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from codex_workbench.credential_schema import (
    CredentialSchemaError, MAX_PAYLOAD_BYTES, MAX_PROBE_BYTES, MAX_SHAPE_BYTES,
    MAX_SHAPE_GROUPS, PAYLOAD_FIELDS, SCHEMA_NAME, build_payload_index,
    describe_structure, normalize_payload, probe_shapes, probe_structure, validate_structure,
)


class CredentialShapesTest(unittest.TestCase):
    """验证格式与映射分离、分组边界、诊断字节预算和旧索引契约。"""

    def shapes(self, value):
        return probe_shapes(json.dumps(value).encode())

    def test_known_format_does_not_promote_unmapped_text_or_json(self):
        cases = (b"synthetic arbitrary text", b'"synthetic JSON string"', b"123", b"true",
                 b"null", b"[]", b"{}", b'{"synthetic-name":"synthetic-value"}',
                 b'{"credential":{"synthetic-name":"synthetic-value"}}')
        for raw in cases:
            with self.subTest(raw_type=probe_structure(raw)["structure_format"]):
                index = probe_structure(raw)
                before = copy.deepcopy(index)
                self.assertEqual({"format_status": "known", "mapping_status": "unmapped"}, describe_structure(index))
                self.assertEqual(("unknown", [], "unrecognized"),
                                 (index["kind"], index["has_fields"], index["structure_status"]))
                self.assertEqual(before, index)
                self.assertEqual(index, validate_structure(index))

    def test_mapping_status_distinguishes_partial_empty_and_errors(self):
        cases = ((build_payload_index({"password": "synthetic"}), "known", "mapped"),
                 (build_payload_index({}), "known", "empty"),
                 (probe_structure(b'{"password":"synthetic","unmapped":0}'), "known", "partial"),
                 (probe_structure(b'{"password":false}'), "known", "error"),
                 (probe_structure(b"\xff"), "unknown", "error"))
        for index, format_status, mapping in cases:
            self.assertEqual({"format_status": format_status, "mapping_status": mapping}, describe_structure(index))

    def test_summary_requires_current_strict_index_and_is_not_an_index(self):
        for value in (None, {"structure_status": "stale"}, {"structure_status": "unindexed"},
                      {**build_payload_index({}), "synthetic-unknown-key": "synthetic-value"}):
            with self.assertRaises(CredentialSchemaError):
                describe_structure(value)
        with self.assertRaises(CredentialSchemaError):
            validate_structure(self.shapes({"password": "synthetic"}))

    def test_array_objects_remain_separate_candidates(self):
        result = self.shapes({"credentials": [{"account": "synthetic-user"}, {"password": "synthetic-password"}]})
        self.assertEqual(("unmapped", "unknown", []),
                         (result["mapping_status"], result["structure"]["kind"], result["structure"]["has_fields"]))
        self.assertEqual((3, 1), (result["object_count"], result["array_count"]))
        first, second = result["groups"][1:]
        self.assertEqual({"account": ["string"]}, first["known_fields"])
        self.assertEqual({"password": ["string"]}, second["known_fields"])
        self.assertEqual((1, 1), (first["parent_id"], second["parent_id"]))
        self.assertNotEqual(first["object_id"], second["object_id"])
        self.assertEqual(("candidate", "candidate"), (first["mapping_status"], second["mapping_status"]))

    def test_alias_collisions_stay_within_one_object(self):
        separate = self.shapes([{"account": "synthetic-a"}, {"username": "synthetic-b"}])
        self.assertTrue(all(group["structure_error"] is None for group in separate["groups"]))
        duplicate = self.shapes([{"account": "synthetic-a", "username": "synthetic-b"}])
        group = duplicate["groups"][0]
        self.assertEqual(("error", "duplicate_field"), (group["mapping_status"], group["structure_error"]))
        self.assertEqual({"account": ["string", "string"]}, group["known_fields"])
        self.assertEqual("unmapped", duplicate["mapping_status"])

    def test_unknown_containers_and_root_fields_never_merge(self):
        marker = "synthetic-private-name-and-value"
        result = self.shapes({"endpoint": marker, marker: {"password": marker, "username": marker},
                              "target": {"host": marker}})
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(["endpoint"], result["structure"]["has_fields"])
        self.assertEqual("credential", result["structure"]["kind"])
        self.assertEqual("partial", result["mapping_status"])
        root, nested, target = result["groups"]
        self.assertEqual(("root", "other", "target"), (root["container"], nested["container"], target["container"]))
        self.assertEqual({"account": ["string"], "password": ["string"]}, nested["known_fields"])
        self.assertEqual((1, 1), (nested["parent_id"], target["parent_id"]))
        self.assertEqual(1, result["unknown_key_count"])

    def test_nested_wrappers_provide_evidence_without_promoting_fields(self):
        payload = {"credentials": [{"credential": {"user": "synthetic", "accessToken": "synthetic"}},
                                    {"credential": {"secret": "synthetic", "hostname": "synthetic"}}]}
        result = self.shapes(payload)
        candidates = [group for group in result["groups"] if group["container"] == "credential"]
        self.assertEqual(2, len(candidates))
        self.assertEqual({"account": ["string"], "token": ["string"]}, candidates[0]["known_fields"])
        self.assertEqual({"host": ["string"], "secret": ["string"]}, candidates[1]["known_fields"])
        self.assertNotEqual(candidates[0]["parent_id"], candidates[1]["parent_id"])
        self.assertEqual("unmapped", result["mapping_status"])
        self.assertEqual([], result["structure"]["has_fields"])
        direct = self.shapes(payload["credentials"][0])
        self.assertEqual(("mapped", "token"), (direct["mapping_status"], direct["structure"]["kind"]))

    def test_candidate_type_errors_do_not_turn_into_valid_mappings(self):
        result = self.shapes([{"password": {"synthetic-private-key": "synthetic-private-value"}},
                              {"account": None, "port": "synthetic-port"}])
        error, child, valid = result["groups"]
        self.assertEqual(("error", "invalid_field_type"), (error["mapping_status"], error["structure_error"]))
        self.assertEqual("unmapped", child["mapping_status"])
        self.assertEqual("candidate", valid["mapping_status"])
        self.assertEqual({"account": ["null"], "port": ["string"]}, valid["known_fields"])
        self.assertNotIn("synthetic-private", json.dumps(result))

    def test_full_counts_and_explicit_truncation_bound_large_diagnostics(self):
        # 仍在 4096 个解析节点内；大量同形对象不能造成无限 stdout。
        # 扩展后的允许字段仍保持在 4096 节点解析上限内，以测试输出截断。
        count = min(150, 4090 // (len(PAYLOAD_FIELDS) + 1))
        payload = [{name: "synthetic" for name in PAYLOAD_FIELDS - {"port", "database_index"}} for _ in range(count)]
        result = self.shapes(payload)
        self.assertEqual((count, 1, "truncated"),
                         (result["object_count"], result["array_count"], result["shape_status"]))
        self.assertLessEqual(len(result["groups"]), MAX_SHAPE_GROUPS)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) + 1, MAX_SHAPE_BYTES)
        self.assertEqual("unmapped", result["mapping_status"])

    def test_probe_error_gates_and_plain_text_do_not_produce_candidate_groups(self):
        invalid = (b"\xff", b'{"secret":"synthetic","secret":"synthetic"}',
                   b'{"password":NaN}', b"x" * (MAX_PROBE_BYTES + 1),
                   json.dumps([{}] * 4096).encode())
        for raw in invalid:
            result = probe_shapes(raw)
            self.assertEqual(("error", "unavailable", []),
                             (result["mapping_status"], result["shape_status"], result["groups"]))
        result = probe_shapes(b"synthetic arbitrary text")
        self.assertEqual(("known", "unmapped", "unavailable"),
                         (result["format_status"], result["mapping_status"], result["shape_status"]))

    def test_new_payload_fields_size_and_version_contract_are_unchanged(self):
        self.assertEqual({"description", "account", "password", "ip", "remote_path", "port", "endpoint", "key",
                          "host", "token", "private_key", "public_key", "passphrase", "certificate",
                          "database_type", "database", "namespace", "group", "access_key", "secret_key", "bucket", "region", "app_id",
                          "database_index", "connection_mode", "endpoints", "sentinel_master", "auth_database", "replica_set", "connection_uri",
                          "brokers", "security_protocol", "sasl_mechanism", "virtual_host", "name_servers", "kubeconfig", "transport_security", "extra_config"}, PAYLOAD_FIELDS)
        overhead = len(b'{"password":""}')
        payload = {"password": "x" * (MAX_PAYLOAD_BYTES - overhead)}
        self.assertEqual(payload, normalize_payload(payload))
        with self.assertRaises(CredentialSchemaError):
            normalize_payload({"password": payload["password"] + "x"})
        for version in (2, True, "1"):
            result = self.shapes({"schema": SCHEMA_NAME, "version": version, "credential": {}})
            self.assertEqual("invalid_schema", result["structure"]["structure_error"])
            self.assertEqual("unavailable", result["shape_status"])
        result = self.shapes({"schema": SCHEMA_NAME, "version": 1, "credential": {"token": "synthetic"}})
        self.assertEqual("indexed", result["structure"]["structure_status"])
        self.assertEqual(["token"], result["structure"]["has_fields"])

    def test_default_consumer_remains_eight_keys_and_shapes_mode_is_explicit(self):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        marker = "synthetic-private-name-and-value"
        raw = json.dumps({marker: [{"account": marker}, {"password": marker}]}).encode()
        command = [sys.executable, "-B", "-m", "codex_workbench.credential_probe"]
        for arguments in ([], ["--shapes"], [marker]):
            process = subprocess.run(command + arguments, input=raw, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=environment, timeout=5)
            self.assertEqual(b"", process.stderr)
            self.assertNotIn(marker.encode(), process.stdout)
            self.assertLessEqual(len(process.stdout), MAX_SHAPE_BYTES)
            result = json.loads(process.stdout)
            if arguments == [marker]:
                self.assertEqual(1, process.returncode)
                self.assertEqual({"error": "structure_probe_failed"}, result)
            else:
                self.assertEqual(0, process.returncode)
                if arguments:
                    self.assertEqual("unmapped", result["mapping_status"])
                    self.assertEqual(3, result["object_count"])
                else:
                    self.assertEqual(8, len(result))
                    self.assertEqual(result, validate_structure(result))


if __name__ == "__main__":
    unittest.main()
