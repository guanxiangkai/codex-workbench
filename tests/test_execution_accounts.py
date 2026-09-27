"""执行账户配置引用与任务快照的持久层测试。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from codex_workbench.store import Store, StoreError


class ExecutionAccountStoreTest(unittest.TestCase):
    """验证每次领取采用当前默认账户，且目录引用始终处于本机私有边界。"""

    def setUp(self) -> None:
        """建立隔离数据库、项目和代理；测试目录不含任何认证材料。"""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "workbench.db")
        self.project = self.store.create_project("演示项目", str(self.root))
        self.agent = self.store.create_agent("执行代理")

    def tearDown(self) -> None:
        """移除测试临时目录。"""
        self.temp.cleanup()

    def account_home(self, name: str) -> Path:
        """创建一个满足 0700 要求的空本机目录。"""
        path = self.root / name
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
        return path

    def ready_task(self, account_id: str | None = None) -> dict:
        """创建并置为 ready 的自动任务。"""
        task = self.store.create_task(
            self.project["id"], "执行任务", agent_id=self.agent["id"], execution_account_id=account_id
        )
        return self.store.update_task(task["id"], task["version"], state="ready")

    def assert_error(self, code: str, callable_: object, *args: object, **kwargs: object) -> None:
        """断言持久层错误码。"""
        with self.assertRaises(StoreError) as caught:
            callable_(*args, **kwargs)  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)

    def test_default_switch_retargets_existing_tasks_and_preserves_run_snapshots(self) -> None:
        """任务每次领取都使用当前默认账户，已经开始的运行仍保持其快照。"""
        account_a = self.store.create_execution_account("账户 A", str(self.account_home("account-a")))
        account_b = self.store.create_execution_account("账户 B", str(self.account_home("account-b")))
        self.store.set_default_execution_account(account_a["id"])
        task_a = self.ready_task()
        self.store.set_default_execution_account(account_b["id"])
        self.assertEqual(account_b["id"], self.store.get_task(task_a["id"])["execution_account_id"])
        run_a = self.store.claim(task_a["id"])
        self.assertEqual(
            {"id": account_b["id"], "name": "账户 B", "codex_home": str(self.account_home("account-b")),
             "kind": "isolated", "subject_id": None},
            run_a["execution_account"],
        )
        self.store.finish(run_a["id"], "review")
        self.store.set_default_execution_account(account_a["id"])
        self.assertEqual(account_b["id"], self.store.list_runs(task_a["id"])[0]["execution_account_id"])
        task_b = self.store.create_task(self.project["id"], "新任务", agent_id=self.agent["id"])
        self.assertEqual(account_a["id"], task_b["execution_account_id"])
        accounts = self.store.list_execution_accounts()
        self.assertEqual([True, False], [account["isDefault"] for account in accounts])

    def test_bound_native_session_uses_its_storage_account_after_default_switch(self) -> None:
        account_a = self.store.create_execution_account("账户 A", str(self.account_home("account-a")))
        account_b = self.store.create_execution_account("账户 B", str(self.account_home("account-b")))
        self.store.record_account_subject(account_a["id"], "subject-a")
        self.store.record_account_subject(account_b["id"], "subject-b")
        self.store.set_default_execution_account(account_a["id"])
        session = self.store.sessions.create(
            self.project["id"], None, str(self.root), account_a["id"], "subject-a",
            "read-only", 1, "never",
        )
        self.store.sessions.attach_thread(
            session["id"], "12345678-1234-1234-1234-123456789abc",
            account_id=account_a["id"], account_subject="subject-a",
        )
        self.store.set_default_execution_account(account_b["id"])
        task = self.store.create_task(self.project["id"], "延续会话", agent_id=self.agent["id"], session_id=session["id"])
        task = self.store.update_task(task["id"], task["version"], state="ready")
        run = self.store.claim(task["id"])
        self.assertEqual(account_b["id"], run["execution_account"]["id"])
        self.assertEqual(account_a["id"], run["session_storage_account"]["id"])

    def test_explicit_unknown_account_does_not_fall_back_to_default(self) -> None:
        """显式给出未知账户必须失败，不能静默改用默认账户。"""
        account = self.store.create_execution_account("账户 A", str(self.account_home("account-a")))
        self.store.set_default_execution_account(account["id"])
        self.assert_error(
            "not_found",
            self.store.create_task,
            self.project["id"],
            "未知账户任务",
            agent_id=self.agent["id"],
            execution_account_id="missing-account",
        )

    def test_explicit_task_account_is_validated_but_cannot_override_default(self) -> None:
        """旧字段可继续传入，却不能把任务固定到非默认账户。"""
        account_a = self.store.create_execution_account("账户 A", str(self.account_home("account-a")))
        account_b = self.store.create_execution_account("账户 B", str(self.account_home("account-b")))
        self.store.set_default_execution_account(account_a["id"])
        task = self.ready_task(account_a["id"])
        changed = self.store.update_task(task["id"], task["version"], execution_account_id=account_b["id"])
        self.assertEqual(account_a["id"], changed["execution_account_id"])
        self.store.set_default_execution_account(account_b["id"])
        run = self.store.claim(changed["id"])
        self.assertEqual(account_b["id"], run["execution_account"]["id"])

    def test_rejects_symlink_cloud_default_native_and_shared_paths(self) -> None:
        """符号链接、云盘、默认根目录和权限过宽目录不能登记为执行账户。"""
        private = self.account_home("private")
        linked = self.root / "linked"
        os.symlink(private, linked)
        self.assert_error("validation", self.store.create_execution_account, "链接", str(linked))
        cloud = self.root / "Library" / "Mobile Documents" / "account"
        cloud.mkdir(parents=True, mode=0o700)
        cloud.chmod(0o700)
        self.assert_error("validation", self.store.create_execution_account, "云盘", str(cloud))
        shared = self.account_home("shared")
        shared.chmod(0o750)
        self.assert_error("validation", self.store.create_execution_account, "共享", str(shared))
        native = self.root / ".codex"
        native.mkdir(mode=0o700)
        native.chmod(0o700)
        with mock.patch("codex_workbench.account_runtime.Path.home", return_value=self.root):
            self.assert_error("validation", self.store.create_execution_account, "原生", str(native))

    def test_v0_tasks_migrate_with_null_execution_account(self) -> None:
        """旧数据库加列后保留既有任务，execution_account_id 必须仍为 NULL。"""
        legacy_path = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL,
                    agent_id TEXT, state TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO tasks VALUES ('legacy-task', 'legacy-project', '旧任务', '', NULL, 'backlog', 1, 'now', 'now');
                """
            )
        Store(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            execution_account_id = conn.execute(
                "SELECT execution_account_id FROM tasks WHERE id = 'legacy-task'"
            ).fetchone()[0]
        self.assertIn("execution_account_id", columns)
        self.assertIsNone(execution_account_id)


if __name__ == "__main__":
    unittest.main()
