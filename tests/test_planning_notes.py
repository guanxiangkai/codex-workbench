import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from codex_workbench.planning_notes import PlanningNotes


class PlanningNotesTest(unittest.TestCase):
    def detail(self, task_id, title="Task"):
        return {"id": task_id, "title": title, "period": "daily", "status": "pending",
                "prompt": "Write it", "runs": [], "assets": [],
                "knowledge_refs": [{"scope": "work", "key": "k"}]}

    def test_export_and_update_with_asset_link(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY,path TEXT,name TEXT)")
            task_id = str(uuid4())
            db.execute("INSERT INTO assets VALUES(?,?,?)", ("a1", "asset-a1-note.png", "note.png"))
            notes = PlanningNotes(Path(directory), db)
            first = notes.export({**self.detail(task_id), "assets": [{"id": "a1"}]})
            self.assertEqual("created", first["status"])
            self.assertIn("../files/asset-a1-note.png", Path(first["path"]).read_text())
            self.assertIn('file.inFolder("Tasks")', (Path(directory) / "工作台任务.base").read_text())
            self.assertIn('file.inFolder("Assets")', (Path(directory) / "资料库索引.base").read_text())
            second = notes.export(self.detail(task_id, "Changed"))
            self.assertEqual("updated", second["status"])

    def test_manual_edit_is_conflict_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(":memory:")
            notes = PlanningNotes(Path(directory), db)
            task_id = str(uuid4())
            result = notes.export(self.detail(task_id))
            path = Path(result["path"])
            path.write_text("# user draft\n", encoding="utf-8")
            conflict = notes.export(self.detail(task_id, "Changed"))
            self.assertEqual("conflict", conflict["status"])
            self.assertEqual("# user draft\n", path.read_text())

    def test_symlink_target_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(":memory:")
            notes = PlanningNotes(Path(directory), db)
            task_id = str(uuid4())
            target = notes.tasks_root / f"{task_id}.md"
            target.symlink_to(Path(directory) / "outside.md")
            with self.assertRaises(ValueError):
                notes.export(self.detail(task_id))

    def test_symlink_directory_and_encoded_asset_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            vault.mkdir()
            external = Path(directory) / "external"
            external.mkdir()
            (vault / "Tasks").symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError):
                PlanningNotes(vault, sqlite3.connect(":memory:"))

        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY,path TEXT,name TEXT)")
            task_id = str(uuid4())
            db.execute("INSERT INTO assets VALUES(?,?,?)", ("a1", "asset-a1-note file(1).png", "[图片] 1.png"))
            notes = PlanningNotes(Path(directory), db)
            result = notes.export({**self.detail(task_id), "prompt": "第一行\n第二行", "assets": [{"id": "a1"}]})
            content = Path(result["path"]).read_text()
            self.assertIn("第一行\n第二行", content)
            self.assertIn("%20", content)


if __name__ == "__main__":
    unittest.main()
