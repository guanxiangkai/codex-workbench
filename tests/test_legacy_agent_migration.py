"""旧专业助手表升级到当前任务执行模型的回归测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_workbench.store import Store, StoreError


class LegacyAgentMigrationTests(unittest.TestCase):
    """只使用临时数据库，验证旧助手记录及其历史引用在重建后仍完整。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "legacy.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _legacy_database(self, *, broken_binding: bool = False) -> None:
        binding_agent = "missing-agent" if broken_binding else "agent-1"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, cwd TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE execution_accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, codex_home TEXT NOT NULL UNIQUE, version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE conversation_sessions(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, project_id TEXT NOT NULL,
                    section_id TEXT, cwd TEXT NOT NULL, execution_account_id TEXT, account_subject TEXT,
                    native_thread_id TEXT, source TEXT NOT NULL CHECK(source IN ('workbench','codex')),
                    sandbox TEXT CHECK(sandbox IN ('read-only','workspace-write','danger-full-access')),
                    concurrency INTEGER CHECK(concurrency BETWEEN 1 AND 32), approval_policy TEXT,
                    native_title_sync TEXT NOT NULL DEFAULT 'unbound' CHECK(native_title_sync IN ('unbound','unknown','synced','conflict')),
                    version INTEGER NOT NULL CHECK(version>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    model TEXT, effort TEXT, thread_account_id TEXT, thread_account_subject TEXT
                );
                CREATE TABLE agents(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                    model TEXT CHECK(model IS NULL OR length(model) BETWEEN 1 AND 120),
                    effort TEXT CHECK(effort IS NULL OR effort IN ('low','medium','high')),
                    instructions TEXT NOT NULL CHECK(length(instructions) <= 30000),
                    sandbox TEXT NOT NULL CHECK(sandbox IN ('read-only','workspace-write')),
                    concurrency INTEGER NOT NULL CHECK(concurrency BETWEEN 1 AND 32),
                    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, description TEXT
                );
                CREATE TABLE tasks(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), agent_id TEXT REFERENCES agents(id),
                    sandbox TEXT NOT NULL CHECK(sandbox IN ('read-only','workspace-write','danger-full-access')),
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE runs(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), project_id TEXT NOT NULL REFERENCES projects(id),
                    agent_id TEXT REFERENCES agents(id), execution_sandbox TEXT NOT NULL CHECK(execution_sandbox IN ('read-only','workspace-write','danger-full-access')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE agent_models(agent_id TEXT NOT NULL REFERENCES agents(id), model_id TEXT NOT NULL, PRIMARY KEY(agent_id, model_id));
                CREATE INDEX agents_legacy_description ON agents(description);
                CREATE TRIGGER agents_restricted_name BEFORE INSERT ON agents WHEN NEW.name='受限助手' BEGIN SELECT RAISE(ABORT, 'synthetic agent constraint'); END;
            """)
            conn.execute("INSERT INTO projects VALUES ('project-1','旧项目',?,?)", (self.temp.name, "2026-01-01T00:00:00Z"))
            conn.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "agent-1", "旧专业助手", "legacy-model", "high", "旧规则", "workspace-write", 7, 4,
                "2026-01-01T00:00:00Z", "旧说明",
            ))
            conn.execute("INSERT INTO tasks VALUES ('task-1','project-1','agent-1','workspace-write','review','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO runs VALUES ('run-1','task-1','project-1','agent-1','workspace-write','2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO agent_models VALUES (?, 'model-1')", (binding_agent,))

    def test_migrates_legacy_agents_without_losing_history_or_model_binding(self) -> None:
        self._legacy_database()

        store = Store(self.path)
        created = store.create_agent("新专业助手", "新规则", "新说明")

        self.assertEqual("新专业助手", created["name"])
        with sqlite3.connect(self.path) as conn:
            legacy = conn.execute("""SELECT id,name,model,effort,instructions,description,sandbox,concurrency,version,created_at
                FROM agents WHERE id='agent-1'""").fetchone()
            self.assertEqual(("agent-1", "旧专业助手", "legacy-model", "high", "旧规则", "旧说明", "workspace-write", 7, 4, "2026-01-01T00:00:00Z"), legacy)
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(agents)")}
            self.assertEqual(0, columns["sandbox"][3])
            self.assertEqual(0, columns["concurrency"][3])
            self.assertEqual("agent-1", conn.execute("SELECT agent_id FROM tasks WHERE id='task-1'").fetchone()[0])
            self.assertEqual("agent-1", conn.execute("SELECT agent_id FROM runs WHERE id='run-1'").fetchone()[0])
            self.assertEqual(("agent-1", "model-1"), conn.execute("SELECT agent_id,model_id FROM agent_models").fetchone())
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM agent_models").fetchone()[0])
            self.assertIn("agents_legacy_description", {row[1] for row in conn.execute("PRAGMA index_list(agents)")})
            self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))
        with self.assertRaises(StoreError) as caught:
            store.create_agent("受限助手")
        self.assertEqual("constraint", caught.exception.code)

    def test_rolls_back_when_legacy_foreign_keys_are_invalid(self) -> None:
        self._legacy_database(broken_binding=True)

        with self.assertRaises(StoreError) as caught:
            Store(self.path)

        self.assertEqual("migration_invalid", caught.exception.code)
        with sqlite3.connect(self.path) as conn:
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(agents)")}
            self.assertEqual(1, columns["sandbox"][3])
            self.assertEqual(1, columns["concurrency"][3])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0])
            self.assertEqual("workspace-write", conn.execute("SELECT sandbox FROM agents WHERE id='agent-1'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
