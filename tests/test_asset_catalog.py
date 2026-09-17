"""技能素材目录的受限读取和预览安全测试。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.asset_catalog import AssetCatalog, AssetCatalogError


class AssetCatalogTest(unittest.TestCase):
    """所有技能来源均为合成目录，不接触用户资源或网络。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "demo-skill"
        self.root.mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# Demo", encoding="utf-8")
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.catalog = AssetCatalog(lambda: [{"id": "demo", "name": "演示", "path": str(self.skill)}])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_catalog(self, entries: list[dict]) -> None:
        (self.assets / "catalog.json").write_text(json.dumps({"assets": entries}), encoding="utf-8")

    def test_catalog_metadata_is_public_but_paths_and_contents_are_not(self) -> None:
        icon = self.assets / "icons" / "ok.svg"
        icon.parent.mkdir()
        icon.write_text('<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>', encoding="utf-8")
        content = icon.read_bytes()
        self.write_catalog([{ "path": "assets/icons/ok.svg", "kind": "icon", "name": "确认", "source": "https://example.test/ok.svg", "license": "ISC", "sha256": hashlib.sha256(content).hexdigest()}])
        listed = self.catalog.list()
        self.assertFalse(listed["truncated"])
        item = listed["assets"][0]
        self.assertEqual("assets/icons/ok.svg", item["relative_path"])
        self.assertNotIn(str(self.root), json.dumps(listed, ensure_ascii=False))
        self.assertNotIn("<svg", json.dumps(listed))
        detail = self.catalog.detail(item["id"])
        self.assertEqual("image", detail["preview"]["kind"])
        self.assertTrue(detail["preview"]["data_uri"].startswith("data:image/svg+xml;base64,"))

    def test_no_catalog_falls_back_to_bounded_safe_static_assets(self) -> None:
        (self.assets / "note.md").write_text("正文", encoding="utf-8")
        (self.assets / ".env").write_text("no", encoding="utf-8")
        (self.assets / "api-key.txt").write_text("no", encoding="utf-8")
        (self.assets / "private.pem").write_text("no", encoding="utf-8")
        deep = self.assets / "a" / "b" / "c" / "d" / "e" / "deep.txt"
        deep.parent.mkdir(parents=True)
        deep.write_text("too deep", encoding="utf-8")
        listed = self.catalog.list()
        self.assertEqual(["assets/note.md"], [item["relative_path"] for item in listed["assets"]])
        self.assertEqual("正文", self.catalog.detail(listed["assets"][0]["id"])["preview"]["text"])

    def test_catalog_rejects_traversal_secret_and_external_symlink(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        linked = self.assets / "linked.txt"
        linked.symlink_to(outside)
        (self.assets / "good.txt").write_text("ok", encoding="utf-8")
        self.write_catalog([{ "path": "../outside.txt" }, {"path": "assets/secret.txt"}, {"path": "assets/linked.txt"}, {"path": "assets/good.txt"}])
        self.assertEqual(["assets/good.txt"], [item["relative_path"] for item in self.catalog.list()["assets"]])
        with self.assertRaises(AssetCatalogError):
            self.catalog.detail("asset_forged")

    def test_external_catalog_directory_link_is_not_read(self) -> None:
        """清单目录本身也不能把发现范围带到技能根外。"""
        self.assets.rmdir()
        outside = Path(self.temp.name) / "outside-assets"
        outside.mkdir()
        (outside / "catalog.json").write_text(json.dumps({"assets": [{"path": "assets/leak.txt"}]}), encoding="utf-8")
        (outside / "leak.txt").write_text("outside", encoding="utf-8")
        self.assets.symlink_to(outside, target_is_directory=True)
        self.assertEqual([], self.catalog.list()["assets"])

    def test_bad_svg_hash_mismatch_and_large_assets_have_no_executable_preview(self) -> None:
        bad_svg = self.assets / "bad.svg"
        bad_svg.write_text('<svg><script>alert(1)</script></svg>', encoding="utf-8")
        text = self.assets / "changed.txt"
        text.write_text("changed", encoding="utf-8")
        large = self.assets / "large.png"
        large.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        self.write_catalog([
            {"path": "assets/bad.svg"},
            {"path": "assets/changed.txt", "sha256": "0" * 64},
            {"path": "assets/large.png"},
        ])
        items = {item["relative_path"]: item for item in self.catalog.list()["assets"]}
        self.assertEqual("metadata", self.catalog.detail(items["assets/bad.svg"]["id"])["preview"]["kind"])
        mismatch = self.catalog.detail(items["assets/changed.txt"]["id"])
        self.assertEqual("integrity_failed", mismatch["asset"]["status"])
        self.assertEqual("metadata", mismatch["preview"]["kind"])
        self.assertEqual("too_large", items["assets/large.png"]["status"])
        self.assertEqual("metadata", self.catalog.detail(items["assets/large.png"]["id"])["preview"]["kind"])

    def test_read_rechecks_identity_after_content_read(self) -> None:
        file = self.assets / "race.txt"
        file.write_text("first", encoding="utf-8")
        self.write_catalog([{ "path": "assets/race.txt" }])
        asset_id = self.catalog.list()["assets"][0]["id"]
        import os
        original_open = os.open
        changed = False

        def racing_open(path: Path, *args, **kwargs):
            nonlocal changed
            handle = original_open(path, *args, **kwargs)
            if Path(path) == file.resolve() and not changed:
                changed = True
                file.write_text("second", encoding="utf-8")
            return handle

        with patch("codex_workbench.asset_catalog.os.open", racing_open):
            with self.assertRaisesRegex(AssetCatalogError, "changed.*reading"):
                self.catalog.detail(asset_id)

    def test_hard_link_outside_resource_authority_is_not_indexed(self):
        import os
        external=self.root/'outside.json'
        external.write_text('{"password":"synthetic-private"}')
        os.link(external,self.assets/'public.json')
        self.write_catalog([{'path':'assets/public.json'}])
        self.assertEqual([],self.catalog.list()['assets'])

    def test_font_preview_uses_verified_content_and_rejects_large_or_invalid_fonts(self):
        """字体沿用内容校验和读取上限；损坏字体不生成伪造字形。"""
        font = self.assets / "sample.woff2"
        content = b"synthetic-font"
        font.write_bytes(content)
        self.write_catalog([{"path": "assets/sample.woff2", "sha256": hashlib.sha256(content).hexdigest()}])
        asset_id = self.catalog.list()["assets"][0]["id"]
        with patch("codex_workbench.asset_catalog._font_preview", return_value="data:image/png;base64,eA==") as draw:
            result = self.catalog.detail(asset_id)
            draw.assert_called_once_with(content)
            self.assertEqual("image", result["preview"]["kind"])
        self.assertEqual("unpreviewable", self.catalog.detail(asset_id)["asset"]["status"])
        font.write_bytes(b"changed")
        with patch("codex_workbench.asset_catalog._font_preview") as draw:
            self.assertEqual("integrity_failed", self.catalog.detail(asset_id)["asset"]["status"])
            draw.assert_not_called()
        font.write_bytes(b"x" * (1024 * 1024 + 1))
        self.assertEqual("too_large", self.catalog.detail(asset_id)["asset"]["status"])

    def test_limits_are_reported_without_crossing_skill_cap(self) -> None:
        entries = []
        for index in range(201):
            file = self.assets / f"item-{index}.txt"
            file.write_text("x", encoding="utf-8")
            entries.append({"path": f"assets/{file.name}"})
        self.write_catalog(entries)
        listed = self.catalog.list()
        self.assertEqual(200, len(listed["assets"]))
        self.assertTrue(listed["truncated"])


if __name__ == "__main__":
    unittest.main()
