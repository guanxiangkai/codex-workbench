"""运行协调的竞态、限额和失败恢复测试。"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_workbench.executor import CodexExecutor, Execution
from codex_workbench.runner import Runner
from codex_workbench.store import Store


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "board.db"))
        account = self.store.create_execution_account("测试执行账户", self.temp.name)
        self.store.set_default_execution_account(account["id"])
        project = self.store.create_project("测试", self.temp.name)
        agent = self.store.create_agent("测试")
        self.task = self.store.create_task(
            project["id"], "任务", agent_id=agent["id"], model="task-model", effort="high",
            concurrency=3, sandbox="workspace-write",
        )
        self.store.update_task(self.task["id"], self.task["version"], state="ready")

    def await_terminal(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.store.get_task(self.task["id"])
            if task["state"] != "running":
                return task
            time.sleep(0.01)
        self.fail("运行未结束")

    def test_progress_limit_does_not_abort_execution(self):
        class Verbose:
            def execute(inner, request, cancel, on_event):
                for _ in range(250):
                    on_event("progress", "完成一步")
                return Execution("review", "完成")
        runner = Runner(self.store, Verbose())
        self.addCleanup(runner.close)
        run = runner.start(self.task["id"])
        self.assertEqual(self.await_terminal()["state"], "done")
        events = self.store.list_events(run["id"])
        self.assertEqual(len(events), 200)
        self.assertEqual(events[-1]["kind"], "notice")

    def test_runner_uses_task_execution_settings_not_agent_configuration(self):
        observed = []
        class Inspecting:
            def execute(inner, request, cancel, on_event):
                observed.append(request)
                return Execution("review", "完成")
        runner = Runner(self.store, Inspecting())
        self.addCleanup(runner.close)
        self.assertEqual("running", runner.start(self.task["id"])["state"])
        self.assertEqual(self.await_terminal()["state"], "done")
        request = observed[0]
        self.assertEqual(("task-model", "high", 3, "workspace-write"),
                         (request.model, request.effort, request.concurrency, request.sandbox))

    def test_missing_execution_account_never_starts_executor(self):
        unconfigured = Store(str(Path(self.temp.name) / "unconfigured.db"))
        project = unconfigured.create_project("未配置", self.temp.name)
        agent = unconfigured.create_agent("测试")
        task = unconfigured.create_task(project["id"], "任务", agent_id=agent["id"])
        unconfigured.update_task(task["id"], task["version"], state="ready")
        class ForbiddenExecutor:
            def execute(inner, *args):
                raise AssertionError("未设置默认账户不得调用任何执行器")
        runner = Runner(unconfigured, ForbiddenExecutor())
        self.addCleanup(runner.close)
        with self.assertRaisesRegex(ValueError, "默认执行账户"):
            runner.start(task["id"])
        self.assertEqual(unconfigured.list_runs(task["id"]), [])

    def test_real_executor_requires_gateway_before_claiming(self):
        runner = Runner(self.store, CodexExecutor(("unused",)))
        self.addCleanup(runner.close)
        with mock.patch("codex_workbench.account_routing.gateway_endpoint", side_effect=ValueError("网关未就绪")):
            with self.assertRaisesRegex(ValueError, "网关未就绪"):
                runner.start(self.task["id"])
        self.assertEqual("ready", self.store.get_task(self.task["id"])["state"])
        self.assertEqual([], self.store.list_runs(self.task["id"]))

    def test_real_executor_receives_checked_gateway_endpoint(self):
        observed = []
        class RecordingExecutor(CodexExecutor):
            def execute(inner, request, cancel, on_event):
                observed.append(request)
                return Execution("review", "完成")
        runner = Runner(self.store, RecordingExecutor(("unused",)))
        self.addCleanup(runner.close)
        with mock.patch("codex_workbench.account_routing.gateway_endpoint", return_value="http://127.0.0.1:43123/v1"):
            runner.start(self.task["id"])
        self.assertEqual("done", self.await_terminal()["state"])
        self.assertEqual("http://127.0.0.1:43123/v1", observed[0].gateway_url)

    def test_cancel_before_finalization_wins_over_success(self):
        entered, release = threading.Event(), threading.Event()
        class Delayed:
            def execute(inner, request, cancel, on_event):
                entered.set()
                release.wait(3)
                return Execution("review", "完成")
        runner = Runner(self.store, Delayed())
        self.addCleanup(runner.close)
        run = runner.start(self.task["id"])
        self.assertTrue(entered.wait(2))
        runner.cancel(run["id"])
        release.set()
        self.assertEqual(self.await_terminal()["state"], "done")

    def test_cancel_after_finalization_is_rejected(self):
        entered, release = threading.Event(), threading.Event()
        finish = self.store.finish
        def blocked_finish(*args, **kwargs):
            entered.set()
            release.wait(3)
            return finish(*args, **kwargs)
        self.store.finish = blocked_finish
        class Fast:
            def execute(inner, *args):
                return Execution("review", "完成")
        runner = Runner(self.store, Fast())
        self.addCleanup(runner.close)
        run = runner.start(self.task["id"])
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ValueError):
            runner.cancel(run["id"])
        release.set()
        self.assertEqual(self.await_terminal()["state"], "done")


if __name__ == "__main__":
    unittest.main()
