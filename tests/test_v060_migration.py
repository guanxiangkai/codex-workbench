"""0.5 旧沙箱枚举数据库升级到 0.6 的合成迁移回归。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_workbench.store import Store


class V060MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workbench.sqlite3"
        self.ids = {name: str(uuid.uuid4()) for name in ("project", "account", "session", "task", "run", "event")}
        self._old_database()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _old_database(self) -> None:
        ids = self.ids
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE local_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,cwd TEXT NOT NULL UNIQUE,section_name TEXT,section_id TEXT,color TEXT,icon TEXT,native_id TEXT UNIQUE,is_workspace INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
                CREATE TABLE sections(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,color TEXT,icon TEXT,created_at TEXT NOT NULL);
                CREATE TABLE agents(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,model TEXT,effort TEXT,instructions TEXT NOT NULL,description TEXT,sandbox TEXT NOT NULL DEFAULT 'read-only',concurrency INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
                CREATE TABLE execution_accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,codex_home TEXT NOT NULL UNIQUE,kind TEXT NOT NULL DEFAULT 'isolated',subject_id TEXT,display_name TEXT,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
                CREATE TABLE preferences(singleton INTEGER PRIMARY KEY,default_execution_account_id TEXT REFERENCES execution_accounts(id));
                CREATE TABLE conversation_sessions(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,project_id TEXT NOT NULL,section_id TEXT,cwd TEXT NOT NULL,execution_account_id TEXT NOT NULL,account_subject TEXT NOT NULL,native_thread_id TEXT,source TEXT NOT NULL,
                    sandbox TEXT,concurrency INTEGER,approval_policy TEXT,version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE tasks(
                    id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,prompt TEXT NOT NULL,agent_id TEXT REFERENCES agents(id),execution_account_id TEXT REFERENCES execution_accounts(id),execution_account_subject TEXT,
                    resource_paths TEXT NOT NULL DEFAULT '[]',section_name TEXT,section_id TEXT,model TEXT,effort TEXT,concurrency INTEGER NOT NULL DEFAULT 1,
                    sandbox TEXT NOT NULL DEFAULT 'read-only' CHECK(sandbox IN ('read-only','workspace-write')),session_id TEXT REFERENCES conversation_sessions(id),
                    state TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE runs(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),project_id TEXT NOT NULL REFERENCES projects(id),agent_id TEXT REFERENCES agents(id),state TEXT NOT NULL,
                    task_title TEXT NOT NULL,task_prompt TEXT NOT NULL,project_name TEXT NOT NULL,project_cwd TEXT NOT NULL,agent_name TEXT NOT NULL,agent_instructions TEXT NOT NULL,
                    execution_model TEXT,execution_effort TEXT,execution_concurrency INTEGER NOT NULL DEFAULT 1,
                    execution_sandbox TEXT NOT NULL DEFAULT 'read-only' CHECK(execution_sandbox IN ('read-only','workspace-write')),
                    execution_account_id TEXT REFERENCES execution_accounts(id),execution_account_name TEXT,execution_account_home TEXT,execution_account_kind TEXT,execution_account_subject TEXT,session_id TEXT REFERENCES conversation_sessions(id),
                    thread_id TEXT,turn_id TEXT,result TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',instructions_snapshot TEXT NOT NULL DEFAULT '',resource_manifest TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,finished_at TEXT
                );
                CREATE TABLE events(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX runs_task_created ON runs(task_id,created_at);
                CREATE INDEX events_run_created ON events(run_id,created_at);
                CREATE UNIQUE INDEX one_running_task_per_session ON tasks(session_id) WHERE session_id IS NOT NULL AND state='running';
            """)
            conn.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?)", (ids["project"], "合成项目", self.temp.name, None, None, None, None, None, 0, "2026-01-01T00:00:00Z"))
            conn.execute("INSERT INTO execution_accounts VALUES (?,?,?,?,?,?,?,?)", (ids["account"], "合成账户", self.temp.name + "/account", "isolated", "subject", None, 1, "2026-01-01T00:00:00Z"))
            conn.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ids["session"], "合成会话", "", ids["project"], None, self.temp.name, ids["account"], "subject", None, "workbench", "workspace-write", 1, "never", 1, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ids["task"], ids["project"], "合成任务", "合成正文", None, ids["account"], "subject", "[]", None, None, None, None, 1, "workspace-write", ids["session"], "review", 2, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ids["run"], ids["task"], ids["project"], None, "review", "合成任务", "合成正文", "合成项目", self.temp.name, "默认助手", "", None, None, 1, "workspace-write", ids["account"], "合成账户", self.temp.name + "/account", "isolated", "subject", ids["session"], "thread", None, "结果", "", "", "[]", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"))
            conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (ids["event"], ids["run"], "progress", "合成事件", "2026-01-01T00:00:00Z"))

    def test_preserves_legacy_rows_session_foreign_keys_indexes_and_is_idempotent(self) -> None:
        first = Store(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual("workspace-write", conn.execute("SELECT sandbox FROM tasks WHERE id=?", (self.ids["task"],)).fetchone()[0])
            self.assertEqual("workspace-write", conn.execute("SELECT execution_sandbox FROM runs WHERE id=?", (self.ids["run"],)).fetchone()[0])
            self.assertEqual(self.ids["session"], conn.execute("SELECT session_id FROM tasks WHERE id=?", (self.ids["task"],)).fetchone()[0])
            self.assertEqual(self.ids["session"], conn.execute("SELECT session_id FROM runs WHERE id=?", (self.ids["run"],)).fetchone()[0])
            self.assertEqual(self.ids["run"], conn.execute("SELECT run_id FROM events WHERE id=?", (self.ids["event"],)).fetchone()[0])
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(tasks)")}
            self.assertIn("one_running_task_per_session", indexes)
            self.assertIn("runs_task_created", {row[1] for row in conn.execute("PRAGMA index_list(runs)")})
            self.assertIn("events_run_created", {row[1] for row in conn.execute("PRAGMA index_list(events)")})
            self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))
            conn.execute("UPDATE tasks SET sandbox='danger-full-access' WHERE id=?", (self.ids["task"],))
            conn.execute("UPDATE runs SET execution_sandbox='danger-full-access' WHERE id=?", (self.ids["run"],))
        second = Store(self.path)
        self.assertEqual(self.ids["task"], first.get_task(self.ids["task"])["id"])
        self.assertEqual(self.ids["task"], second.get_task(self.ids["task"])["id"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
