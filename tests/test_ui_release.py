"""UI 发布快照的原子读取与失败保留回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.ui_release import UiRelease, build_release


class UiReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.release = Path(self.temp.name) / "release.json"
        self.first = build_release()
        self.release.write_text(json.dumps(self.first), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_release_file_is_atomic_snapshot_and_source_builder_is_not_used(self):
        with patch("codex_workbench.ui_release.build_release", side_effect=AssertionError("production must not load source")):
            view = UiRelease(release_file=self.release)
            original = view.manifest("workbench")
            self.assertEqual(self.first["revision"], original["revision"])
            changed = json.loads(json.dumps(self.first))
            changed["revision"] = "a" * 64
            for page in changed["pages"].values():
                page["revision"] = changed["revision"]
                page["html"] = page["html"].replace("codex-workbench-ui", "codex-workbench-ui-release")
            self.release.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(changed["revision"], view.manifest("workbench")["revision"])

    def test_invalid_or_unreadable_release_keeps_last_good_snapshot(self):
        view = UiRelease(release_file=self.release)
        previous = view.manifest("workbench")
        bad = json.loads(json.dumps(self.first)); bad["version"] = "mismatch"
        self.release.write_text(json.dumps(bad), encoding="utf-8")
        self.assertEqual(previous, view.manifest("workbench"))
        self.release.write_text("{broken", encoding="utf-8")
        self.assertEqual(previous, view.manifest("workbench"))

    def test_release_uses_revisioned_resource_uri_for_the_workbench_entry(self):
        page = self.first["pages"]["workbench"]
        entry = next(tool for tool in page["tools"] if tool["name"] == page["entry"])
        self.assertEqual(f"ui://codex-workbench/v7/workbench/{self.first['revision']}.html", page["resource_uri"])
        self.assertEqual(page["resource_uri"], entry["_meta"]["ui"]["resourceUri"])
