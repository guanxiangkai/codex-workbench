"""执行 JSONL 指标提取与 SQLite 运行记录的合成回归测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from codex_workbench.executor import CodexExecutor, Execution, Request
from codex_workbench.runner import Runner
from codex_workbench.store import Store, StoreError


class RunMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_executor_extracts_real_completed_usage_and_location_fields(self):
        thread_id, turn_id, item_id = (str(uuid.uuid4()) for _ in range(3))
        script = self.root / "metrics_cli.py"
        events = [
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "turn.started", "turn_id": turn_id},
            {"type": "item.completed", "item": {"id": item_id, "type": "agent_message", "text": "合成结果"}},
            {"type": "turn.completed", "turn_id": turn_id, "usage": {"input_tokens": 120, "output_tokens": 34, "cached_input_tokens": 56}},
        ]
        script.write_text("import json,sys\nsys.stdin.read()\n" + "\n".join("print(" + repr(json.dumps(event)) + ",flush=True)" for event in events), encoding="utf-8")
        result = CodexExecutor((sys.executable, str(script))).execute(Request(str(self.root), "测试", "指标", account_home=str(self.root)), threading.Event(), lambda *_: None)
        self.assertEqual((thread_id, turn_id, item_id, 120, 34, 56),
                         (result.thread_id, result.turn_id, result.item_id, result.input_tokens, result.output_tokens, result.cached_input_tokens))

    def test_missing_or_invalid_usage_stays_null_and_runner_records_real_duration(self):
        script = self.root / "missing_usage.py"
        thread_id = str(uuid.uuid4())
        script.write_text("import json,sys\nsys.stdin.read()\n" + "\n".join([
            "print(json.dumps({'type':'thread.started','thread_id':'" + thread_id + "'}),flush=True)",
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'完成'}}),flush=True)",
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':'bad','output_tokens':-1}}),flush=True)",
        ]), encoding="utf-8")
        outcome = CodexExecutor((sys.executable, str(script))).execute(Request(str(self.root), "测试", "指标", account_home=str(self.root)), threading.Event(), lambda *_: None)
        self.assertEqual((None, None, None), (outcome.input_tokens, outcome.output_tokens, outcome.cached_input_tokens))

        store = Store(self.root / "workbench.sqlite3")
        account_home = self.root / "account"
        account_home.mkdir(mode=0o700)
        account = store.create_execution_account("合成账户", str(account_home))
        store.set_default_execution_account(account["id"])
        project = store.create_project("合成项目", str(self.root))
        task = store.create_task(project["id"], "任务", execution_account_id=account["id"])
        task = store.update_task(task["id"], task["version"], state="ready")

        class Timed:
            def execute(self, request, cancel, on_event):
                on_event("thread", thread_id)
                time.sleep(0.01)
                return Execution("review", "完成", thread_id=thread_id, turn_id="turn-1", item_id="item-1")

        runner = Runner(store, Timed())
        self.addCleanup(runner.close)
        run = runner.start(task["id"])
        deadline = time.monotonic() + 2
        while store.get_task(task["id"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        saved = store.list_runs(task["id"])[0]
        self.assertGreaterEqual(saved["duration_ms"], 1)
        self.assertEqual((None, None, None), (saved["input_tokens"], saved["output_tokens"], saved["cached_input_tokens"]))
        self.assertEqual(("turn-1", "item-1"), (saved["turn_id"], saved["item_id"]))
        with self.assertRaises(StoreError):
            store.record_run_metrics(run["id"], duration_ms=1)

    def test_nonzero_exit_preserves_received_usage_and_locations(self):
        thread_id, turn_id, item_id = (str(uuid.uuid4()) for _ in range(3))
        script = self.root / "nonzero_metrics.py"
        events = [
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "turn.started", "turn_id": turn_id},
            {"type": "item.completed", "item": {"id": item_id, "type": "agent_message", "text": "已产生结果"}},
            {"type": "turn.completed", "turn_id": turn_id, "usage": {"input_tokens": 9, "output_tokens": 8, "cached_input_tokens": 7}},
        ]
        script.write_text("import json,sys\nsys.stdin.read()\n" + "\n".join("print(" + repr(json.dumps(event)) + ",flush=True)" for event in events) + "\nsys.exit(7)\n", encoding="utf-8")
        result = CodexExecutor((sys.executable, str(script))).execute(Request(str(self.root), "测试", "指标", account_home=str(self.root)), threading.Event(), lambda *_: None)
        self.assertEqual("failed", result.state)
        self.assertEqual((turn_id, item_id, 9, 8, 7),
                         (result.turn_id, result.item_id, result.input_tokens, result.output_tokens, result.cached_input_tokens))
