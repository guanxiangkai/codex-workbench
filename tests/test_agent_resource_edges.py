"""仅以临时合成资源验证目录边界及原始文本摘要契约。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from codex_workbench.agent_resources import ResourceError, ResourceLibrary


class AgentResourceEdgesTest(unittest.TestCase):
    """拒绝父目录链接，并保留可编辑资源的原始字节语义。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.library = ResourceLibrary(self.base / "resources", self.base / "locks")
        self.agent = {"id": str(uuid.uuid4()), "name": "合成助手", "instructions": "初始规则"}
        self.directory = Path(self.library.ensure(self.agent)["directory"])

    def replace_agents_with_link(self) -> Path:
        """把合成 agents 目录移出资源根并用链接替换。"""
        outside = self.base / "outside"
        agents = self.library.root / "agents"
        agents.rename(outside)
        agents.symlink_to(outside, target_is_directory=True)
        return outside

    def test_read_rejects_linked_agents_parent(self) -> None:
        self.replace_agents_with_link()
        with self.assertRaises(ResourceError):
            self.library.read_text(self.agent["id"], "instructions.md")

    def test_ensure_rejects_parent_link_before_creating_outside_directory(self) -> None:
        outside = self.replace_agents_with_link()
        other = {"id": str(uuid.uuid4()), "instructions": "不得越界创建"}
        with self.assertRaises(ResourceError):
            self.library.ensure(other)
        self.assertFalse((outside / other["id"]).exists())

    def test_snapshot_parent_open_rejects_ancestor_replaced_after_validation(self) -> None:
        target = self.directory / "templates" / "示例.md"
        target.write_text("合成模板", encoding="utf-8")
        verify = self.library._verify_parent_components

        def replace_after_check(path: Path) -> None:
            verify(path)
            self.replace_agents_with_link()

        with patch.object(self.library, "_verify_parent_components", side_effect=replace_after_check):
            with self.assertRaises(ResourceError):
                fd = self.library._open_parent(target)
                # 旧实现会沿祖先链接打开目录；关闭后让 assertRaises 报错。
                os.close(fd)

    def test_unicode_empty_and_crlf_text_round_trip_with_read_digest(self) -> None:
        path = "prompts/中文 空白.md"
        target = self.directory / path
        for raw in (b"", "第一行\r\n第二行\r\n".encode("utf-8")):
            with self.subTest(raw=raw):
                target.write_bytes(raw)
                text = self.library.read_text(self.agent["id"], path)
                self.assertEqual(raw, text.encode("utf-8"))
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                result = self.library.write_text(self.agent["id"], path, "修改后", digest)
                self.assertEqual(hashlib.sha256("修改后".encode()).hexdigest(), result["sha256"])

    def test_editing_agent_preserves_every_user_owned_resource(self) -> None:
        """更新助手元数据只能更新投影，不得重置规则和资源仓库状态。"""
        contents = {"instructions.md": b"", "knowledge.json": b'{"references":["synthetic"]}',
                    "prompts/提示.md": "人工提示".encode(), "templates/空.json": b"",
                    "assets/example.bin": b"\xff", "history/synthetic.json": b"{}",
                    "README.md": b"user readme", "unknown.bin": b"keep"}
        for path, raw in contents.items():
            (self.directory / path).write_bytes(raw)
        self.library.ensure({**self.agent, "name": "更新名称", "instructions": "不可覆盖现有空规则"})
        for path, raw in contents.items():
            with self.subTest(path=path):
                self.assertEqual(raw, (self.directory / path).read_bytes())


if __name__ == "__main__":
    unittest.main()
