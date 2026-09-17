"""业务会话登记、原生线程绑定与任务聚合的合成 SQLite 测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_workbench.sessions import SessionError, SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workbench.sqlite3"
        self.registry = SessionRegistry(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create(self, **changes):
        fields = {"project_id": "project-1", "section_id": "section-1", "cwd": self.temp.name,
                  "execution_account_id": "account-1", "account_subject": "subject-1", "sandbox": "danger-full-access",
                  "concurrency": 3, "approval_policy": "never", "title": "", "description": ""}
        return self.registry.create(**(fields | changes))

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(SessionError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_workbench_session_fixes_policy_and_updates_only_display_fields(self):
        session = self.create()
        self.assertEqual(("workbench", "danger-full-access", 3, "never"),
                         (session["source"], session["sandbox"], session["concurrency"], session["approval_policy"]))
        updated = self.registry.update_title(session["id"], session["version"], title="模型总结标题", description="会话说明")
        self.assertEqual(("模型总结标题", "会话说明", 2), (updated["title"], updated["description"], updated["version"]))
        self.error("version_conflict", self.registry.update_title, session["id"], session["version"], title="旧写入")

    def test_native_title_readback_is_authoritative_and_exposes_conflict(self):
        session = self.create(title="工作台标题")
        self.registry.attach_thread(session["id"], str(uuid.uuid4()))
        bound = self.registry.get(session["id"])
        self.assertEqual("unknown", bound["native_title_sync"])
        conflict = self.registry.sync_native_title(bound["id"], bound["version"], "工作台请求标题", "原生并发标题")
        self.assertEqual(("原生并发标题", "conflict"), (conflict["title"], conflict["native_title_sync"]))
        synced = self.registry.sync_native_title(conflict["id"], conflict["version"], "原生确认标题", "原生确认标题")
        self.assertEqual(("原生确认标题", "synced"), (synced["title"], synced["native_title_sync"]))

    def test_native_title_observation_never_leaves_bound_title_claimed_as_unbound(self):
        session = self.create(title="本地旧标题")
        self.registry.attach_thread(session["id"], str(uuid.uuid4()))
        observed = self.registry.observe_native_title(session["id"], "原生标题")
        self.assertEqual(("原生标题", "conflict"), (observed["title"], observed["native_title_sync"]))
        self.error("validation", self.registry.sync_native_title, self.create()["id"], 1, "标题", "标题")

    def test_native_is_idempotent_and_never_invents_policy(self):
        thread = str(uuid.uuid4())
        native = self.registry.ensure_native(thread, "project-1", "section-1", self.temp.name, "current", "subject-1", title="原生会话")
        same = self.registry.ensure_native(thread, "project-1", "section-1", self.temp.name, "current", "subject-1")
        self.assertEqual(native["id"], same["id"])
        self.assertEqual(("codex", thread, None, None, None),
                         (native["source"], native["native_thread_id"], native["sandbox"], native["concurrency"], native["approval_policy"]))
        self.error("conflict", self.registry.ensure_native, thread, "project-1", "section-1", self.temp.name, "current", "other-subject")

    def test_native_import_reuses_bound_workbench_session_and_updates_section_only(self):
        session = self.create(section_id="old-section")
        thread = str(uuid.uuid4())
        self.registry.attach_thread(session["id"], thread)
        imported = self.registry.ensure_native(thread, "project-1", "new-section", self.temp.name, "account-1", "subject-1")
        self.assertEqual(session["id"], imported["id"])
        self.assertEqual(("workbench", "new-section"), (imported["source"], imported["section_id"]))
        self.error("conflict", self.registry.ensure_native, thread, "other-project", "new-section", self.temp.name, "account-1", "subject-1")

    def test_thread_binding_is_immutable_and_can_join_caller_transaction(self):
        session = self.create()
        thread, other = str(uuid.uuid4()), str(uuid.uuid4())
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            self.registry.attach_thread(session["id"], thread, connection=conn)
            conn.rollback()
        self.assertIsNone(self.registry.get(session["id"])["native_thread_id"])
        self.assertEqual(thread, self.registry.attach_thread(session["id"], thread)["native_thread_id"])
        self.assertEqual(thread, self.registry.attach_thread(session["id"], thread)["native_thread_id"])
        self.error("conflict", self.registry.attach_thread, session["id"], other)
        second = self.create()
        self.error("conflict", self.registry.attach_thread, second["id"], thread)

    def test_list_aggregates_tasks_only_after_store_adds_session_id(self):
        session = self.create()
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,session_id TEXT,state TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?,?,?)", [("t1", session["id"], "running"), ("t2", session["id"], "ready")])
        listed = self.registry.list("project-1")
        self.assertEqual((2, 1), (listed[0]["task_count"], listed[0]["running_count"]))
