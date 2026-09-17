"""仅用旧版合成 SQLite 快照验证凭证目录升级和结构索引恢复。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_workbench.credential_schema import build_payload_index
from codex_workbench.credentials import CredentialCatalog, CredentialCatalogError


class CredentialMigrationRecoveryTest(unittest.TestCase):
    """保留旧组织资料，且恢复过程不能把失效索引当作当前结构。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entries = [
            {"entryId": "legacy-account", "revision": 10},
            {"entryId": "legacy-model", "revision": 3},
            {"entryId": "unorganized", "revision": 1},
        ]
        self.operations: list[str] = []
        runner = patch.object(CredentialCatalog, "_run", autospec=True, side_effect=self._vault)
        runner.start()
        self.addCleanup(runner.stop)

    def _vault(self, catalog: CredentialCatalog, operation: str):
        # 所有入口在此终止；不执行 CLI，不读取任何真实保险库文件。
        self.assertTrue(catalog.db_path.is_relative_to(self.root))
        self.operations.append(operation)
        if operation == "status":
            return {"ready": True, "entries": len(self.entries)}
        self.assertEqual("list", operation)
        return [dict(entry) for entry in self.entries]

    def _legacy(self, name: str, layout: int = 0) -> Path:
        path = self.root / name
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE credential_folders(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    parent_id TEXT REFERENCES credential_folders(id), color TEXT);
                CREATE TABLE credential_organizer(
                    entry_id TEXT PRIMARY KEY, label TEXT,
                    folder_id TEXT REFERENCES credential_folders(id),
                    tags_json TEXT NOT NULL DEFAULT '[]', color TEXT,
                    kind TEXT NOT NULL DEFAULT 'other',
                    base_urls_json TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE credential_targets(
                    entry_id TEXT PRIMARY KEY, target_digest TEXT NOT NULL);
                INSERT INTO credential_folders VALUES
                    ('root', '合成目录', NULL, '#123456'),
                    ('child', '合成子目录', 'root', NULL),
                    ('empty', '空目录也须保留', NULL, NULL);
                INSERT INTO credential_organizer VALUES
                    ('legacy-account', '自定义账户', 'child', '["中文", "保留顺序"]',
                     '#abcdef', 'ssh', '[]'),
                    ('legacy-model', '自定义模型', 'root', '["模型"]',
                     NULL, 'api_key', '["https://synthetic.invalid/v1"]'),
                    ('absent-from-vault', '暂未列出的旧记录', 'child', '["保留"]',
                     NULL, 'password', '[]');
                INSERT INTO credential_targets VALUES ('legacy-model', 'synthetic-target-digest');
            """)
            if layout >= 1:
                conn.execute("ALTER TABLE credential_organizer ADD COLUMN has_fields_json TEXT NOT NULL DEFAULT '[]'")
                conn.execute("UPDATE credential_organizer SET has_fields_json='[\"password\"]'")
            if layout >= 2:
                conn.execute("ALTER TABLE credential_organizer ADD COLUMN organization_source TEXT")
                conn.execute("UPDATE credential_organizer SET organization_source='manual'")
        return path

    def _catalog(self, path: Path) -> CredentialCatalog:
        return CredentialCatalog(path, self.root / "never-execute-vault", poll_seconds=60)

    def _stored(self, path: Path, table: str, columns: str = "*") -> list[tuple]:
        # 表和列均为测试内常量，不能接收外部输入。
        with sqlite3.connect(path) as conn:
            return conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1").fetchall()

    def _structure(self, path: Path, payload: str | bytes, revision: int = 9, generation: int | float | str = 7) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO credential_structure VALUES (?,?,?,?,?)",
                ("legacy-account", revision, generation, "probe", payload),
            )

    def _entry(self, catalog: CredentialCatalog) -> dict:
        return next(entry for entry in catalog.list()["entries"] if entry["id"] == "legacy-account")

    def _error(self, code: str, action, *args, **kwargs) -> None:
        with self.assertRaises(CredentialCatalogError) as caught:
            action(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_upgrade_each_legacy_layout_preserves_every_original_row_and_reference(self) -> None:
        """覆盖缺两列、缺来源列及已有两列的旧库，包含空目录和清单外记录。"""
        for layout in range(3):
            with self.subTest(layout=layout):
                path = self._legacy(f"layout-{layout}.sqlite3", layout)
                with sqlite3.connect(path) as conn:
                    columns = ",".join(row[1] for row in conn.execute("PRAGMA table_info(credential_organizer)"))
                organizer = self._stored(path, "credential_organizer", columns)
                folders = self._stored(path, "credential_folders")
                targets = self._stored(path, "credential_targets")
                for _ in range(2):
                    catalog = self._catalog(path)
                    result = catalog.list()
                    self.assertEqual(organizer, self._stored(path, "credential_organizer", columns))
                    self.assertEqual(folders, self._stored(path, "credential_folders"))
                    self.assertEqual(targets, self._stored(path, "credential_targets"))
                    self.assertEqual([], self._stored(path, "credential_structure"))
                    self.assertCountEqual(
                        ["vault:legacy-account", "vault:legacy-model", "vault:unorganized"],
                        [entry["reference"] for entry in result["entries"]],
                    )
                    account = self._entry(catalog)
                    self.assertEqual(("自定义账户", "child", ["中文", "保留顺序"], "#abcdef"),
                                     (account["label"], account["folder_id"], account["tags"], account["color"]))
                    for entry in result["entries"]:
                        self.assertEqual(("unindexed", "unknown", []),
                                         (entry["structure_status"], entry["kind"], entry["has_fields"]))
                    exported = catalog.metadata_export()
                    self.assertCountEqual(["legacy-account", "legacy-model", "absent-from-vault"],
                                          [entry["entry_id"] for entry in exported["entries"]])
                    self.assertEqual(3, len(exported["folders"]))
                    self.assertNotIn("synthetic.invalid", json.dumps([result, exported]))
                with sqlite3.connect(path) as conn:
                    self.assertEqual([("ok",)], conn.execute("PRAGMA integrity_check").fetchall())
                    self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual({"status", "list"}, set(self.operations))

    def test_reopened_snapshot_hides_both_older_and_future_revisions_and_missing_revision(self) -> None:
        """恢复旧快照或回退清单时都不能复用不同修订的字段证据。"""
        structure = json.dumps(build_payload_index({"password": "synthetic-only"}))
        for revision in (8, 10, None):
            with self.subTest(revision=revision):
                path = self._legacy(f"revision-{revision}.sqlite3")
                self._catalog(path)
                self._structure(path, structure)
                self.entries[0] = {"entryId": "legacy-account"}
                if revision is not None:
                    self.entries[0]["revision"] = revision
                stored = self._stored(path, "credential_structure")
                entry = self._entry(self._catalog(path))
                self.assertEqual(("stale", "unknown", [], {}),
                                 (entry["structure_status"], entry["kind"], entry["has_fields"], entry["field_types"]))
                self.assertEqual((9, 7), (entry["structure_revision"], entry["structure_generation"]))
                self.assertEqual(stored, self._stored(path, "credential_structure"))

    def test_recovery_requires_persisted_generation_and_commits_only_current_revision(self) -> None:
        """恢复后 generation 不重置，拒绝写入不改变旧证据，重建后再次启动仍可见。"""
        path = self._legacy("generation.sqlite3")
        self._catalog(path)
        old = build_payload_index({"password": "synthetic-old"})
        replacement = build_payload_index({"key": "synthetic-new"})
        self._structure(path, json.dumps(old))
        before = self._stored(path, "credential_structure")
        organization = self._stored(path, "credential_organizer")
        catalog = self._catalog(path)
        self._error("structure_conflict", catalog.index_structure, "legacy-account", 10,
                    replacement, expected_generation=0)
        self._error("structure_stale", catalog.index_structure, "legacy-account", 9,
                    replacement, expected_generation=7)
        self.assertEqual(before, self._stored(path, "credential_structure"))
        repaired = catalog.index_structure("legacy-account", 10, replacement, expected_generation=7)
        self.assertEqual(("indexed", "api_key", 10, 8),
                         (repaired["structure_status"], repaired["kind"], repaired["structure_revision"], repaired["structure_generation"]))
        self.assertEqual(repaired, self._entry(self._catalog(path)))
        self.assertEqual(organization, self._stored(path, "credential_organizer"))

    def test_corrupt_persisted_json_is_isolated_and_repairable_without_losing_organization(self) -> None:
        """持久化坏索引不能拖垮全列表，修复须延续已存 CAS generation。"""
        malformed = [("truncated", "{"), ("null", "null"), ("array", '["wrong-shape"]'),
                     ("schema", '{"schema_version":999}'), ("invalid-utf8-blob", b"\xff")]
        digit_limit = sys.get_int_max_str_digits()
        if digit_limit:
            malformed.append(("integer-conversion-limit", "1" * (digit_limit + 1)))
        for number, (case, payload) in enumerate(malformed):
            with self.subTest(case=case):
                path = self._legacy(f"corrupt-{number}.sqlite3")
                self._catalog(path)
                self._structure(path, payload, revision=10)
                before = self._stored(path, "credential_organizer")
                catalog = self._catalog(path)
                result = catalog.list()
                self.assertEqual(3, len(result["entries"]))
                entry = self._entry(catalog)
                self.assertEqual(("error", "invalid_schema", "unknown", [], {}),
                                 (entry["structure_status"], entry["structure_error"], entry["kind"], entry["has_fields"], entry["field_types"]))
                self.assertEqual(7, entry["structure_generation"])
                repaired = catalog.index_structure("legacy-account", 10, build_payload_index({}), expected_generation=7)
                self.assertEqual(("indexed", 8), (repaired["structure_status"], repaired["structure_generation"]))
                self.assertEqual(before, self._stored(path, "credential_organizer"))
                self.assertEqual(repaired, self._entry(self._catalog(path)))

    def test_noninteger_persisted_generation_cannot_claim_current_structure(self) -> None:
        """SQLite INTEGER 亲和性不保证整数；坏版本令牌不能为当前结构背书。"""
        for number, generation in enumerate((1.5, "synthetic-corrupt")):
            with self.subTest(generation=generation):
                path = self._legacy(f"bad-generation-{number}.sqlite3")
                self._catalog(path)
                self._structure(path, json.dumps(build_payload_index({"key": "synthetic-only"})),
                                revision=10, generation=generation)
                before = self._stored(path, "credential_structure")
                entry = self._entry(self._catalog(path))
                self.assertEqual("error", entry["structure_status"])
                self.assertEqual(("unknown", [], {}),
                                 (entry["kind"], entry["has_fields"], entry["field_types"]))
                self.assertEqual(before, self._stored(path, "credential_structure"))

    def test_two_catalogs_upgrade_same_legacy_snapshot_without_duplicate_column_race(self) -> None:
        """强制两个无事务读者取得同一旧表布局，验证升级本身的并发边界。"""
        path = self._legacy("concurrent-upgrade.sqlite3")
        before = self._stored(path, "credential_organizer")
        barrier = threading.Barrier(2)
        original_connect = sqlite3.connect

        class MigrationConnection(sqlite3.Connection):
            """仅控制无事务的 schema 读取时序；已有写事务时不人为阻塞。"""

            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if sql.strip().lower() == "pragma table_info(credential_organizer)" and not self.in_transaction:
                    # 先耗尽游标释放读锁；同步的是观察时刻，不伪造数据库返回值。
                    rows = list(cursor)
                    barrier.wait(timeout=3)
                    return rows
                return cursor

        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=MigrationConnection)

        failures = []
        with patch("codex_workbench.credentials.sqlite3.connect", side_effect=connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._catalog, path) for _ in range(2)]
                for future in futures:
                    try:
                        future.result(timeout=8)
                    except Exception as error:
                        failures.append(f"{type(error).__name__}: {error}")
        self.assertEqual([], failures, "并发升级必须均成功且保留原数据")
        original_columns = "entry_id,label,folder_id,tags_json,color,kind,base_urls_json"
        self.assertEqual(before, self._stored(path, "credential_organizer", original_columns))
        self.assertEqual([], self._stored(path, "credential_structure"))
        self.assertEqual(3, len(self._catalog(path).list()["entries"]))


if __name__ == "__main__":
    unittest.main()
