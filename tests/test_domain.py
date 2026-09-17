"""分区、项目外观与旧数据库迁移契约。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from codex_workbench.store import Store, StoreError


class DomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "workbench.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, callable_: object, *args: object, **kwargs: object) -> None:
        with self.assertRaises(StoreError) as caught:
            callable_(*args, **kwargs)  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)

    def test_sections_are_entities_and_projects_reference_ids(self) -> None:
        section = self.store.create_section("产品", "#a1b2c3", {"kind": "svg", "value": '<svg viewBox="0 0 8 8"><circle cx="4" cy="4" r="3"/></svg>'})
        self.assertEqual("#A1B2C3", section["color"])
        project = self.store.create_project("工作台", str(self.root), section_id=section["id"], color="#001122", icon={"kind": "emoji", "value": "📦"}, native_id="native:project:1")
        self.assertEqual(section["id"], project["section_id"])
        self.assertEqual({"kind": "emoji", "value": "📦"}, project["icon"])

    def test_appearance_rejects_unsafe_svg_and_symbols(self) -> None:
        self.assert_error("validation", self.store.create_section, "坏色", "blue")
        self.assert_error("validation", self.store.create_section, "脚本", None, {"kind": "svg", "value": "<svg><script/></svg>"})
        self.assert_error("validation", self.store.create_section, "链接", None, {"kind": "svg", "value": '<svg><path href="https://bad"/></svg>'})
        self.assert_error("validation", self.store.create_section, "事件", None, {"kind": "svg", "value": '<svg><path onclick="x"/></svg>'})
        self.assert_error("validation", self.store.create_section, "符号", None, {"kind": "symbol", "value": "folder"})

    def test_old_runs_table_is_migrated_to_allow_default_assistant(self) -> None:
        path = self.root / "old.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, cwd TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, model TEXT, effort TEXT, instructions TEXT NOT NULL, sandbox TEXT NOT NULL, concurrency INTEGER NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL, agent_id TEXT, state TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL, agent_id TEXT NOT NULL, state TEXT NOT NULL, task_title TEXT NOT NULL, task_prompt TEXT NOT NULL, project_name TEXT NOT NULL, project_cwd TEXT NOT NULL, agent_name TEXT NOT NULL, agent_model TEXT, agent_effort TEXT, agent_instructions TEXT NOT NULL, agent_sandbox TEXT NOT NULL, agent_concurrency INTEGER NOT NULL, thread_id TEXT, turn_id TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT);
                CREATE TABLE events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
        Store(path)
        with closing(sqlite3.connect(path)) as conn:
            agent_column = next(row for row in conn.execute("PRAGMA table_info(runs)") if row[1] == "agent_id")
        self.assertEqual(0, agent_column[3])

    def test_current_account_is_default_and_subject_is_fixed_on_task(self) -> None:
        account_home = self.root / "current-home"
        account_home.mkdir(mode=0o700)
        account_home.chmod(0o700)
        with mock.patch("codex_workbench.store.current_account_home", return_value=str(account_home)):
            current = self.store.register_current_account("当前账户", str(account_home), "subject-a")
        project = self.store.create_project("当前项目", str(self.root))
        task = self.store.create_task(project["id"], "当前任务")
        self.assertEqual("current", task["execution_account_id"])
        self.assertEqual("subject-a", task["execution_account_subject"])
        task = self.store.update_task(task["id"], task["version"], state="ready")
        run = self.store.claim(task["id"])
        self.assertEqual({"id": "current", "name": "当前账户", "codex_home": str(account_home.resolve()), "kind": "current", "subject_id": "subject-a"}, run["execution_account"])
