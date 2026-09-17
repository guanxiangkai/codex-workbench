"""凭证中心只处理合成保险库清单和本机元数据。"""

from __future__ import annotations

import json
import shlex
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.credentials import CredentialCatalog, CredentialCatalogError
from codex_workbench.credential_schema import build_model_key_index, build_payload_index, probe_structure


class CredentialCatalogTest(unittest.TestCase):
    """假 CLI 仅支持 status/list，验证不会触及解密或写入命令。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entries_file = self.root / "entries.json"
        self.entries_file.write_text(json.dumps([
            {"entryId": "old-password", "revision": 1, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"},
            {"entryId": "model-key", "revision": 2, "createdAt": "2026-01-02T00:00:00Z", "updatedAt": "2026-01-03T00:00:00Z"},
        ]), encoding="utf-8")
        self.calls = self.root / "calls.txt"
        self.command = self.root / "fake-vault.sh"
        self.command.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$1\" >> {shlex.quote(str(self.calls))}\n"
            "if [ \"$1\" = status ]; then printf '%s' '{\"ready\":true,\"entries\":2,\"masterKeyAvailable\":true,\"root\":\"/ignored\"}'; exit 0; fi\n"
            "if [ \"$1\" = list ]; then cat " + shlex.quote(str(self.entries_file)) + "; exit 0; fi\n"
            "exit 64\n",
            encoding="utf-8",
        )
        self.command.chmod(0o700)
        self.catalog = CredentialCatalog(self.root / "organizer.sqlite3", self.command, poll_seconds=60)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def error(self, code, callable, *args, **kwargs) -> None:
        with self.assertRaises(CredentialCatalogError) as caught:
            callable(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_list_merges_only_nonsecret_vault_fields_and_caches(self) -> None:
        first = self.catalog.list()
        second = self.catalog.list()
        self.assertEqual(["status", "list"], self.calls.read_text(encoding="utf-8").splitlines())
        self.assertEqual({"ready": True, "entries": 2, "master_key_available": True}, first["status"])
        password = next(item for item in first["entries"] if item["id"] == "old-password")
        self.assertEqual("unknown", password["kind"])
        self.assertEqual("unindexed", password["structure_status"])
        self.assertEqual("old-password", password["label"])
        self.assertNotIn("root", json.dumps(second))

    def test_folders_metadata_and_target_resolution_are_local_only(self) -> None:
        root = self.catalog.folder_create("生产", color="#087565")
        self.assertEqual(root["id"], self.catalog.folder_create("生产")["id"])
        child = self.catalog.folder_create("推理", root["id"])
        sibling = self.catalog.folder_create("其他", root["id"])
        self.error("folder_exists", self.catalog.folder_update, sibling["id"], name="推理")
        updated = self.catalog.entry_update("model-key", label="推理 Key", folder_id=child["id"], tags=["生产", "推理"],
                                              color="#087565")
        self.catalog.index_structure("model-key", 2, build_model_key_index(), expected_generation=0)
        with sqlite3.connect(self.catalog.db_path) as conn:
            conn.execute("UPDATE credential_organizer SET base_urls_json=? WHERE entry_id='model-key'", (json.dumps(["https://api.example.invalid/v1"]),))
        self.assertNotIn("base_urls", updated)
        self.assertEqual("vault:model-key", self.catalog.resolve_key("model-key", "https://api.example.invalid/v1"))
        self.error("target_mismatch", self.catalog.resolve_key, "model-key", "https://other.example.invalid/v1")
        self.error("folder_cycle", self.catalog.folder_update, root["id"], parent_id=child["id"])
        self.error("entry_invalid", self.catalog.entry_update, "missing", label="不存在")
        exported = self.catalog.metadata_export()
        self.assertNotIn("vault:model-key", json.dumps(exported))
        self.assertNotIn("created_at", json.dumps(exported))
        self.assertNotIn("https://api.example.invalid/v1", json.dumps(exported))
        self.assertNotIn("https://api.example.invalid/v1", json.dumps(self.catalog.list()))
        self.assertEqual("推理 Key", exported["entries"][0]["label"])
        self.assertEqual({"status", "list"}, set(self.calls.read_text(encoding="utf-8").splitlines()))

    def test_register_new_key_refreshes_cache_and_requires_target_binding(self) -> None:
        self.catalog.list()
        self.entries_file.write_text(json.dumps([
            {"entryId": "new-model", "revision": 1, "createdAt": "2026-01-04T00:00:00Z", "updatedAt": "2026-01-04T00:00:00Z"}
        ]), encoding="utf-8")
        registered = self.catalog.register_key("vault:new-model", "新模型", "https://api.example.invalid/v1", created=True)
        self.assertEqual("api_key", registered["kind"])
        self.assertEqual(["endpoint", "key"], registered["has_fields"])
        self.assertEqual("vault:new-model", self.catalog.resolve_key("new-model", "https://api.example.invalid/v1"))
        self.entries_file.write_text(json.dumps([
            {"entryId": "unbound", "revision": 1, "createdAt": "2026-01-04T00:00:00Z", "updatedAt": "2026-01-04T00:00:00Z"}
        ]), encoding="utf-8")
        self.catalog = CredentialCatalog(self.root / "other.sqlite3", self.command, poll_seconds=0)
        self.catalog.index_structure("unbound", 1, build_model_key_index(), expected_generation=0)
        self.error("target_binding_required", self.catalog.resolve_key, "unbound", "https://api.example.invalid/v1")

    def test_depth_tags_and_unknown_commands_are_rejected(self) -> None:
        parent_id = None
        for index in range(8):
            parent_id = self.catalog.folder_create(f"层{index}", parent_id)["id"]
        self.error("folder_depth", self.catalog.folder_create, "第九层", parent_id)
        shallow = self.catalog.folder_create("浅层")
        self.error("folder_depth", self.catalog.folder_update, shallow["id"], parent_id=parent_id)
        self.error("metadata_invalid", self.catalog.entry_update, "model-key", tags=["重复", "重复"])
        with self.assertRaises(TypeError):
            self.catalog.entry_update("model-key", kind="api_key")

    def test_revision_indexing_preserves_custom_organization_and_hides_stale_fields(self) -> None:
        folder = self.catalog.folder_create("用户目录")
        self.catalog.entry_update("old-password", label="自定义", folder_id=folder["id"], tags=["保留"])
        structure = probe_structure(b'{"username":"synthetic-account","password":"synthetic-password"}')
        indexed = self.catalog.index_structure("old-password", 1, structure, expected_generation=0)
        self.assertEqual("password", indexed["kind"])
        self.assertEqual(["account", "password"], indexed["has_fields"])
        self.assertEqual({"account": "string", "password": "string"}, indexed["field_types"])
        self.assertEqual("自定义", indexed["label"])
        self.assertEqual(folder["id"], indexed["folder_id"])
        self.assertEqual(["保留"], indexed["tags"])
        self.assertEqual("probe", indexed["structure_source"])
        self.assertEqual(1, indexed["structure_generation"])
        self.error("structure_conflict", self.catalog.index_structure, "old-password", 1, structure, expected_generation=0)
        self.error("structure_stale", self.catalog.index_structure, "old-password", 2, structure, expected_generation=1)
        entries = json.loads(self.entries_file.read_text())
        entries[0]["revision"] = 2
        self.entries_file.write_text(json.dumps(entries))
        self.catalog._snapshot(force=True)
        stale = self.catalog.list()["entries"][0]
        self.assertEqual("stale", stale["structure_status"])
        self.assertEqual("unknown", stale["kind"])
        self.assertEqual([], stale["has_fields"])
        self.assertEqual({}, stale["field_types"])
        refreshed = self.catalog.index_structure("old-password", 2, structure, expected_generation=1)
        self.assertEqual(2, refreshed["structure_generation"])
        for result in (indexed, refreshed, self.catalog.metadata_export()):
            self.assertNotIn("synthetic-account", json.dumps(result))
            self.assertNotIn("synthetic-password", json.dumps(result))

    def test_legacy_kind_and_fields_do_not_become_structure_evidence(self) -> None:
        self.catalog.entry_update("old-password", label="SSH Root Password")
        with sqlite3.connect(self.catalog.db_path) as conn:
            conn.execute("UPDATE credential_organizer SET kind='ssh',has_fields_json='[\"password\"]' WHERE entry_id='old-password'")
        entry = self.catalog.list()["entries"][0]
        self.assertEqual("unknown", entry["kind"])
        self.assertEqual("unindexed", entry["structure_status"])
        self.assertEqual([], entry["has_fields"])
        with sqlite3.connect(self.catalog.db_path) as conn:
            self.assertEqual(("ssh", '["password"]'), conn.execute("SELECT kind,has_fields_json FROM credential_organizer WHERE entry_id='old-password'").fetchone())

    def test_new_registration_uses_index_contract_and_rejects_old_key_relabelling(self) -> None:
        registered = self.catalog.register_payload("vault:old-password", "新凭证", build_payload_index({"key": "synthetic"}))
        self.assertEqual("api_key", registered["kind"])
        self.assertEqual("created", registered["structure_source"])
        self.assertEqual(["key"], registered["has_fields"])
        self.error("structure_stale", self.catalog.register_key, "vault:model-key", "错误重标", "https://fixture.invalid", created=True)
        self.error("structure_invalid", self.catalog.register_key, "vault:model-key", "错误重标", "https://fixture.invalid", created=False)
        forged = {**build_payload_index({}), "secret-name": "synthetic-private-value"}
        self.error("structure_invalid", self.catalog.index_structure, "model-key", 2, forged, expected_generation=0)

    def test_structure_cas_has_one_winner_across_catalog_instances(self) -> None:
        other = CredentialCatalog(self.catalog.db_path, self.command, poll_seconds=0)
        barrier, outcomes = threading.Barrier(2), []
        def index(catalog):
            barrier.wait(timeout=5)
            try:
                catalog.index_structure("old-password", 1, build_payload_index({}), expected_generation=0)
                outcomes.append("ok")
            except CredentialCatalogError as error:
                outcomes.append(error.code)
        threads = [threading.Thread(target=index, args=(catalog,)) for catalog in (self.catalog, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertCountEqual(["ok", "structure_conflict"], outcomes)

    def test_prefix_suggestions_create_idempotent_hierarchy_without_overwriting_custom_metadata(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
            {"entryId": "account.business-application.agent", "revision": 1},
            {"entryId": "ssh.login.internal", "revision": 1},
            {"entryId": "account.feishu.bot", "revision": 1},
            {"entryId": "quarantine.github.old", "revision": 1},
            {"entryId": "unmapped.credential", "revision": 1},
        ]), encoding="utf-8")
        custom = self.catalog.folder_create("用户目录")
        self.catalog.entry_update("account.business-application.agent", label="自定义显示名", folder_id=custom["id"], tags=["保留"])

        suggestions = {item["id"]: item for item in self.catalog.organization_suggestions()["entries"]}
        self.assertEqual(["开发平台", "代码托管"], suggestions["account.gitea.company"]["suggested_path"])
        self.assertEqual(["基础设施", "主机与 SSH"], suggestions["ssh.login.internal"]["suggested_path"])
        self.assertEqual(["协作", "飞书"], suggestions["account.feishu.bot"]["suggested_path"])
        self.assertEqual(["开发平台", "代码托管"], suggestions["quarantine.github.old"]["suggested_path"])
        self.assertEqual("preserved", suggestions["account.business-application.agent"]["state"])
        self.assertEqual("unclassified", suggestions["unmapped.credential"]["state"])

        applied = self.catalog.apply_organization_suggestions()
        self.assertEqual({"account.gitea.company", "ssh.login.internal", "account.feishu.bot", "quarantine.github.old"}, {item["id"] for item in applied["applied"]})
        self.assertIn("account.business-application.agent", {item["id"] for item in applied["skipped"]})
        self.assertEqual([], self.catalog.apply_organization_suggestions()["applied"])
        entries = {item["id"]: item for item in self.catalog.list()["entries"]}
        self.assertEqual("suggestion", entries["account.gitea.company"]["organization_source"])
        self.assertEqual("自定义显示名", entries["account.business-application.agent"]["label"])
        self.assertEqual(custom["id"], entries["account.business-application.agent"]["folder_id"])
        self.assertEqual(["保留"], entries["account.business-application.agent"]["tags"])
        self.assertNotIn("verified", json.dumps(applied))
        self.assertEqual({"status", "list"}, set(self.calls.read_text(encoding="utf-8").splitlines()))

    def test_explicit_business_and_ai_prefixes_are_complete_without_display_name_inference(self) -> None:
        paths = {
            "account.document-digitization.worker": ["AI 服务", "文档解析"],
            "account.business-component.integration.api-key": ["业务系统", "组件接口"],
            "account.business-platform.shared": ["业务系统", "共享平台"],
            "account.business-application.account": ["业务系统", "应用账户"],
            "account.business-workflow.service": ["业务系统", "工作流服务"],
            "account.business-procurement.admin": ["业务系统", "采购管理"],
            "huggingface.model.read": ["AI 服务", "模型资源"],
            "proposal-ppt-image-demo": ["AI 服务", "PPT 生成"],
            "proposal-ppt-reasoning-demo": ["AI 服务", "PPT 生成"],
            "quarantine.feishu.old": ["协作", "飞书"],
            "quarantine.unknown.old": ["待整理"],
        }
        self.entries_file.write_text(json.dumps([{"entryId": entry_id, "revision": 1} for entry_id in paths]), encoding="utf-8")
        suggestions = self.catalog.organization_suggestions()
        self.assertEqual("entry-prefix-v2", suggestions["rules_version"])
        by_id = {item["id"]: item for item in suggestions["entries"]}
        self.assertEqual(paths, {entry_id: by_id[entry_id]["suggested_path"] for entry_id in paths})
        self.assertTrue(all(by_id[entry_id]["can_apply"] for entry_id in paths))

    def test_selected_suggestion_rejects_unknown_entry_and_leaves_other_entries_unorganized(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
            {"entryId": "account.github.company", "revision": 1},
        ]), encoding="utf-8")
        self.error("entry_invalid", self.catalog.apply_organization_suggestions, ["missing"])
        result = self.catalog.apply_organization_suggestions(["account.github.company"])
        self.assertEqual(["account.github.company"], [item["id"] for item in result["applied"]])
        entries = {item["id"]: item for item in self.catalog.list()["entries"]}
        self.assertIsNone(entries["account.gitea.company"]["folder_id"])
        self.assertEqual("unorganized", entries["account.gitea.company"]["organization_source"])

    def test_concurrent_catalogs_apply_once_and_create_each_folder_path_once(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
            {"entryId": "ssh.login.internal", "revision": 1},
        ]), encoding="utf-8")
        other = CredentialCatalog(self.root / "organizer.sqlite3", self.command, poll_seconds=0)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def apply(catalog: CredentialCatalog) -> None:
            try:
                barrier.wait(timeout=3)
                results.append(catalog.apply_organization_suggestions())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=apply, args=(catalog,)) for catalog in (self.catalog, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(2, sum(len(result["applied"]) for result in results))
        folders = self.catalog.list()["folders"]
        self.assertEqual(4, len(folders))
        self.assertEqual(4, len({(folder["parent_id"], folder["name"]) for folder in folders}))
        self.assertEqual([], self.catalog.apply_organization_suggestions()["applied"])

    def test_stale_preview_does_not_overwrite_manual_edit(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
        ]), encoding="utf-8")
        self.assertTrue(self.catalog.organization_suggestions()["entries"][0]["can_apply"])
        manual_folder = self.catalog.folder_create("人工维护")
        self.catalog.entry_update("account.gitea.company", label="人工名称", folder_id=manual_folder["id"], tags=["人工"])
        result = self.catalog.apply_organization_suggestions(["account.gitea.company"])
        self.assertEqual([], result["applied"])
        self.assertEqual("preserved", result["skipped"][0]["state"])
        entry = self.catalog.list()["entries"][0]
        self.assertEqual("人工名称", entry["label"])
        self.assertEqual(manual_folder["id"], entry["folder_id"])

    def test_apply_rollback_removes_all_new_folders_and_organizer_rows_after_failure(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
            {"entryId": "ssh.login.internal", "revision": 1},
        ]), encoding="utf-8")
        insert = self.catalog._insert_suggested_organization

        def fail_on_second(conn, entry_id, folder_id):
            if entry_id == "ssh.login.internal":
                raise RuntimeError("synthetic interrupted batch")
            return insert(conn, entry_id, folder_id)

        with patch.object(self.catalog, "_insert_suggested_organization", side_effect=fail_on_second):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.catalog.apply_organization_suggestions()
        with sqlite3.connect(self.root / "organizer.sqlite3") as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM credential_folders").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM credential_organizer").fetchone()[0])

    def test_legacy_duplicate_folder_is_preserved_and_stably_reused_without_unique_index(self) -> None:
        self.entries_file.write_text(json.dumps([
            {"entryId": "account.gitea.company", "revision": 1},
        ]), encoding="utf-8")
        legacy_path = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("CREATE TABLE credential_folders(id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT, color TEXT)")
            connection.execute("CREATE TABLE credential_organizer(entry_id TEXT PRIMARY KEY, label TEXT, folder_id TEXT, tags_json TEXT NOT NULL DEFAULT '[]', color TEXT, kind TEXT NOT NULL DEFAULT 'other', base_urls_json TEXT NOT NULL DEFAULT '[]', has_fields_json TEXT NOT NULL DEFAULT '[]', organization_source TEXT)")
            connection.execute("INSERT INTO credential_folders(id,name,parent_id,color) VALUES ('first','开发平台',NULL,NULL),('second','开发平台',NULL,NULL)")
        legacy = CredentialCatalog(legacy_path, self.command, poll_seconds=0)
        result = legacy.apply_organization_suggestions()
        with sqlite3.connect(legacy_path) as connection:
            leaf_parent = connection.execute("SELECT parent_id FROM credential_folders WHERE id=?", (result["applied"][0]["folder_id"],)).fetchone()[0]
            self.assertEqual("first", leaf_parent)
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM credential_folders WHERE name='开发平台' AND parent_id IS NULL").fetchone()[0])
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(credential_folders)")}
            self.assertNotIn("credential_folders_parent_name_unique", indexes)


if __name__ == "__main__":
    unittest.main()
