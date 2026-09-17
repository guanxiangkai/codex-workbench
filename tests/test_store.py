"""Codex 工作台持久层的领域行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_workbench.store import Store, StoreError


class StoreTest(unittest.TestCase):
    """覆盖任务执行设置、助手快照及账户门禁。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "workbench.db")
        account = self.store.create_execution_account("测试执行账户", str(self.root))
        self.store.set_default_execution_account(account["id"])
        self.section = self.store.create_section("工作", "#12ab34", {"kind": "emoji", "value": "🛠️"})
        self.project = self.store.create_project("演示项目", str(self.root), section_id=self.section["id"])
        self.agent = self.store.create_agent("PPT 助手", "保持企业视觉规范。", "沉淀演示文稿规则")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, callable_: object, *args: object, **kwargs: object) -> None:
        with self.assertRaises(StoreError) as caught:
            callable_(*args, **kwargs)  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)

    def ready_task(self, **settings: object) -> dict:
        task = self.store.create_task(self.project["id"], "任务", "执行提示", self.agent["id"], **settings)
        return self.store.update_task(task["id"], task["version"], state="ready")

    def test_task_execution_settings_are_snapshotted_independently_of_agent(self) -> None:
        task = self.ready_task(model="gpt-5.6-terra", effort="high", concurrency=3, sandbox="workspace-write")
        run = self.store.claim(task["id"])
        self.assertEqual({"model": "gpt-5.6-terra", "effort": "high", "concurrency": 3, "sandbox": "workspace-write"}, run["execution"])
        self.assertEqual({"id": self.agent["id"], "name": "PPT 助手", "instructions": "保持企业视觉规范。"}, run["agent"])
        self.assertNotIn("model", self.store.list_agents()[0])
        updated = self.store.update_agent(self.agent["id"], self.agent["version"], instructions="新规则")
        self.assertEqual("新规则", updated["instructions"])
        self.assertEqual("保持企业视觉规范。", self.store.list_runs(task["id"])[0]["agent_instructions"])

    def test_task_without_agent_claims_as_default_codex(self) -> None:
        task = self.store.create_task(self.project["id"], "默认执行", model="gpt-5.6-sol")
        task = self.store.update_task(task["id"], task["version"], state="ready")
        run = self.store.claim(task["id"])
        self.assertEqual({"id": None, "name": "默认助手", "instructions": ""}, run["agent"])
        self.assertEqual("gpt-5.6-sol", run["execution"]["model"])

    def test_task_uses_optimistic_version_and_rejects_running_edit(self) -> None:
        task = self.store.create_task(self.project["id"], "待编辑", agent_id=self.agent["id"])
        updated = self.store.update_task(task["id"], task["version"], title="新标题")
        self.assert_error("version_conflict", self.store.update_task, task["id"], task["version"], title="陈旧覆盖")
        ready = self.store.update_task(task["id"], updated["version"], state="ready")
        self.store.claim(ready["id"])
        renamed = self.store.update_task(ready["id"], ready["version"] + 1, title="运行中改名")
        self.assertEqual("运行中改名", renamed["title"])
        self.assert_error("conflict", self.store.update_task, ready["id"], renamed["version"], prompt="运行中修改需求")

    def test_execution_account_remains_required(self) -> None:
        unbound = Store(self.root / "unbound.db")
        project = unbound.create_project("未绑定项目", str(self.root))
        task = unbound.create_task(project["id"], "任务")
        task = unbound.update_task(task["id"], task["version"], state="ready")
        self.assert_error("account_required", unbound.claim, task["id"])
