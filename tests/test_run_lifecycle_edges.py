"""仅用合成 CLI 与临时 SQLite 验证取消、EOF 和指标保留边界。"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_workbench.executor import CodexExecutor, Request
from codex_workbench.runner import Runner
from codex_workbench.store import Store


class RunLifecycleEdgesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def execute(self, suffix, callback=lambda *_: None, timeout=3):
        events = [
            {"type": "thread.started", "thread_id": "synthetic-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "合成结果"}},
            {"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 5, "cached_input_tokens": 3}},
        ]
        script = self.root / "fake_cli.py"
        script.write_text("import sys,json,os,time,signal,subprocess\nsys.stdin.read()\n" +
                          "\n".join("print(" + repr(json.dumps(event)) + ",flush=True)" for event in events) +
                          "\n" + suffix, encoding="utf-8")
        return CodexExecutor((sys.executable, str(script)), timeout).execute(
            Request(str(self.root), "合成测试", "", account_home=str(self.root)), self.cancel, callback)

    def assert_usage(self, result):
        self.assertEqual((17, 5, 3), (result.input_tokens, result.output_tokens, result.cached_input_tokens))

    def test_callback_failure_after_usage_keeps_metrics_and_fails(self):
        def callback(kind, message):
            if kind == "progress":
                raise RuntimeError("SYNTHETIC_PRIVATE_ERROR")
        result = self.execute("print(json.dumps({'type':'error'}),flush=True)\ntime.sleep(10)\n", callback)
        self.assertEqual("failed", result.state)
        self.assert_usage(result)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", result.error)

    def test_truncated_eof_after_completion_cannot_succeed(self):
        result = self.execute("sys.stdout.write('{\\\"type\\\":');sys.stdout.flush()\n")
        self.assertEqual("failed", result.state)
        self.assertIn("不完整", result.error)
        self.assert_usage(result)

    def test_timeout_after_usage_keeps_metrics(self):
        result = self.execute("os.close(1)\nos.close(2)\ntime.sleep(10)\n", timeout=0.4)
        self.assertEqual("failed", result.state)
        self.assertIn("超时", result.error)
        self.assert_usage(result)

    def test_cancel_drains_usage_already_written_in_later_chunks(self):
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "thread.started", "thread_id": "synthetic-thread"},
            {"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 5, "cached_input_tokens": 3}},
        ]) + "\n"
        script = self.root / "split_cli.py"
        script.write_text("import os,sys,time\nsys.stdin.read()\nos.write(1," +
                          repr(stream.encode()) + ")\ntime.sleep(10)\n", encoding="utf-8")
        read = os.read
        def callback(kind, message):
            if kind == "thread":
                self.cancel.set()
        with mock.patch("codex_workbench.executor.os.read", side_effect=lambda fd, size: read(fd, min(size, 64))):
            result = CodexExecutor((sys.executable, str(script))).execute(
                Request(str(self.root), "合成测试", "", account_home=str(self.root)), self.cancel, callback)
        self.assertEqual("cancelled", result.state)
        self.assert_usage(result)

    def test_cancel_stops_stubborn_descendant_and_keeps_metrics(self):
        heartbeat = self.root / "heartbeat"
        child = ("import signal,time,pathlib\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                 "p=pathlib.Path('heartbeat')\n"
                 "for n in range(150):\n p.write_text(str(n));time.sleep(0.02)\n")
        def callback(kind, message):
            if kind == "progress":
                self.cancel.set()
        result = self.execute(
            "child=subprocess.Popen([sys.executable,'-c'," + repr(child) + "])\n"
            "while not os.path.exists('heartbeat'): time.sleep(0.01)\n"
            "print(json.dumps({'type':'error'}),flush=True)\ntime.sleep(10)\n", callback)
        self.assertEqual("cancelled", result.state)
        self.assert_usage(result)
        self.assertTrue(heartbeat.exists())
        before = heartbeat.read_text()
        time.sleep(0.15)
        self.assertEqual(before, heartbeat.read_text(), "取消返回后子进程仍在写入")

    def test_cancel_racing_executor_exception_finishes_once_with_duration(self):
        store = Store(self.root / "temporary.sqlite3")
        account = store.create_execution_account("合成账户", str(self.root))
        store.set_default_execution_account(account["id"])
        project = store.create_project("合成项目", str(self.root))
        task = store.create_task(project["id"], "合成任务")
        store.update_task(task["id"], task["version"], state="ready")
        entered = threading.Event()
        class Interrupted:
            def execute(self, request, cancel, on_event):
                entered.set()
                if not cancel.wait(2):
                    raise AssertionError("未收到取消")
                raise RuntimeError("合成执行中断")
        finishes = []
        original_finish = store.finish
        def finish(*args, **kwargs):
            finishes.append((args, kwargs))
            return original_finish(*args, **kwargs)
        store.finish = finish
        runner = Runner(store, Interrupted())
        self.addCleanup(runner.close)
        run = runner.start(task["id"])
        self.assertTrue(entered.wait(2))
        runner.cancel(run["id"])
        runner.close()
        saved = store.list_runs(task["id"])[0]
        self.assertEqual("cancelled", saved["state"])
        self.assertEqual(1, len(finishes))
        self.assertIsInstance(saved["duration_ms"], int)
        with self.assertRaises(ValueError):
            runner.cancel(run["id"])


if __name__ == "__main__":
    unittest.main()
