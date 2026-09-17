"""智能体资源库的目录、CAS 与引用边界测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_workbench.agent_resources import ResourceError, ResourceLibrary


class ResourceLibraryTest(unittest.TestCase):
    """使用临时目录验证本地资源不会越界或静默覆盖。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.library = ResourceLibrary(Path(self.temp.name) / "resources")
        self.agent = {"id": str(uuid.uuid4()), "name": "PPT 助手", "instructions": "保持企业视觉规范。", "model": "gpt-test", "concurrency": 1}
        self.created = self.library.ensure(self.agent)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, function, *args, **kwargs) -> None:
        with self.assertRaises(ResourceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_ensure_projects_only_allowed_fields_and_preserves_user_instruction(self) -> None:
        directory = Path(self.created["directory"])
        self.assertTrue((directory / "prompts").is_dir())
        self.assertEqual("保持企业视觉规范。", (directory / "instructions.md").read_text())
        (directory / "instructions.md").write_text("人工修订。", encoding="utf-8")
        (directory / "keep.bin").write_bytes(b"keep")
        self.agent["instructions"] = "数据库中的新指令"
        self.agent["execution_account_id"] = "must-not-project"
        self.library.ensure(self.agent)
        self.assertEqual("人工修订。", (directory / "instructions.md").read_text())
        self.assertEqual(b"keep", (directory / "keep.bin").read_bytes())
        projection = json.loads((directory / "agent.json").read_text(encoding="utf-8"))
        self.assertNotIn("instructions", projection)
        self.assertNotIn("execution_account_id", projection)
        self.assertEqual(self.agent["id"], projection["id"])

    def test_text_write_requires_hash_and_rejects_metadata_or_traversal(self) -> None:
        written = self.library.write_text(self.agent["id"], "prompts/outline.md", "第一页")
        self.assertEqual("第一页", self.library.read_text(self.agent["id"], "prompts/outline.md"))
        self.assert_error("precondition_required", self.library.write_text, self.agent["id"], "prompts/outline.md", "第二页")
        self.assert_error("version_conflict", self.library.write_text, self.agent["id"], "prompts/outline.md", "第二页", "0" * 64)
        changed = self.library.write_text(self.agent["id"], "prompts/outline.md", "第二页", written["sha256"])
        self.assertEqual(hashlib.sha256("第二页".encode()).hexdigest(), changed["sha256"])
        self.assert_error("forbidden", self.library.write_text, self.agent["id"], "agent.json", "{}")
        self.assert_error("validation", self.library.read_text, self.agent["id"], "../auth.json")
        self.assert_error("forbidden", self.library.write_text, self.agent["id"], "assets/.env", "x")
        self.assert_error("version_conflict", self.library.write_text, self.agent["id"], "prompts/new.md", "新文件", "0" * 64)

    def test_publish_rechecks_external_edit_create_and_link_replacement(self) -> None:
        """临时文件已落盘后发生的外部改动必须阻止 CAS 发布并保留外部内容。"""
        first = self.library.write_text(self.agent["id"], "prompts/race.md", "初始")
        rules = Path(self.created["directory"]) / "prompts" / "race.md"

        def edit(path: Path) -> None:
            self.assertEqual(rules, path)
            rules.write_text("外部编辑", encoding="utf-8")

        self.library._before_publish = edit
        try:
            self.assert_error("version_conflict", self.library.write_text, self.agent["id"], "prompts/race.md", "本次保存", first["sha256"])
        finally:
            self.library._before_publish = None
        self.assertEqual("外部编辑", rules.read_text(encoding="utf-8"))

        created = Path(self.created["directory"]) / "templates" / "created.md"
        self.library._before_publish = lambda path: created.write_text("外部创建", encoding="utf-8")
        try:
            self.assert_error("version_conflict", self.library.write_text, self.agent["id"], "templates/created.md", "本次创建")
        finally:
            self.library._before_publish = None
        self.assertEqual("外部创建", created.read_text(encoding="utf-8"))

        latest = hashlib.sha256(rules.read_bytes()).hexdigest()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("外部链接内容", encoding="utf-8")

        def replace_with_link(path: Path) -> None:
            path.unlink()
            path.symlink_to(outside)

        self.library._before_publish = replace_with_link
        try:
            self.assert_error("conflict", self.library.write_text, self.agent["id"], "prompts/race.md", "不应发布", latest)
        finally:
            self.library._before_publish = None
        self.assertTrue(rules.is_symlink())
        self.assertEqual("外部链接内容", rules.read_text(encoding="utf-8"))

    def test_selection_is_bounded_and_builds_prompt_without_authorizing_resource_content(self) -> None:
        self.library.write_text(self.agent["id"], "prompts/style.md", "采用蓝色商务风。")
        self.library.write_text(self.agent["id"], "templates/agenda.json", "{}")
        directory = Path(self.created["directory"])
        (directory / "assets" / "logo.bin").write_bytes(b"logo")
        selected = self.library.resolve_selection(self.agent["id"], ["prompts/style.md", "templates/agenda.json", "assets/logo.bin"])
        self.assertEqual(3, len(selected))
        self.assertTrue(all(Path(item["absolutePath"]).is_absolute() for item in selected))
        instructions = self.library.build_instructions(self.agent["id"], selected)
        self.assertIn("保持企业视觉规范", instructions)
        self.assertIn("采用蓝色商务风", instructions)
        self.assertIn("templates/agenda.json", instructions)
        self.assertIn("不授予外发", instructions)
        self.assert_error("forbidden", self.library.resolve_selection, self.agent["id"], ["instructions.md"])
        self.assert_error("validation", self.library.resolve_selection, self.agent["id"], ["assets/logo.bin"] * 21)

    def test_knowledge_references_are_local_metadata_without_personal_library_read(self) -> None:
        knowledge = self.library.read_text(self.agent["id"], "knowledge.json")
        current_hash = hashlib.sha256(knowledge.encode("utf-8")).hexdigest()
        self.library.write_text(self.agent["id"], "knowledge.json", '{"references":["ppt-brand-v3"]}', current_hash)
        instructions = self.library.build_instructions(self.agent["id"], [])
        self.assertIn("ppt-brand-v3", instructions)
        self.assert_error("validation", self.library.write_text, self.agent["id"], "knowledge.json", "[]", hashlib.sha256(b'{"references":["ppt-brand-v3"]}').hexdigest())

    def test_list_skips_hidden_and_links_and_reports_shared_resources(self) -> None:
        directory = Path(self.created["directory"])
        self.library.write_text(self.agent["id"], "templates/cover.md", "封面")
        shared = Path(self.temp.name) / "resources" / "shared" / "assets" / "brand.txt"
        shared.write_text("品牌", encoding="utf-8")
        (directory / ".private.txt").write_text("不应列出", encoding="utf-8")
        (directory / "assets" / "linked.txt").symlink_to(shared)
        result = self.library.list(self.agent["id"])
        paths = {item["path"] for item in result["files"]}
        self.assertIn("templates/cover.md", paths)
        self.assertIn("shared/assets/brand.txt", paths)
        self.assertNotIn(".private.txt", paths)
        self.assertNotIn("assets/linked.txt", paths)
        self.assertEqual("保持企业视觉规范。", result["instructionPreview"])


if __name__ == "__main__":
    unittest.main()
