"""用独立子进程验证 JSONL 边界，不消费模型额度。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_workbench.executor import CodexExecutor, Request


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name
        self.events = []
        self.cancel = threading.Event()

    def execute(self, source, timeout=5, prompt="测试指令"):
        script = Path(self.cwd) / "fake_cli.py"
        script.write_text(source, encoding="utf-8")
        executor = CodexExecutor((sys.executable, str(script)), timeout)
        return executor.execute(Request(self.cwd, "测试", prompt, account_home=self.cwd), self.cancel,
                                lambda kind, message: self.events.append((kind, message)))

    def test_success_requires_complete_protocol(self):
        result = self.execute('''
import json,sys
message=sys.stdin.read()
assert '测试指令' in message
assert sys.argv[-1]=='-'
assert '--sandbox' in sys.argv
for event in [
 {'type':'thread.started','thread_id':'test-thread'},
 {'type':'turn.started'},
 {'type':'item.completed','item':{'type':'agent_message','text':'完成'}},
 {'type':'turn.completed'}]:
 print(json.dumps(event),flush=True)
''')
        self.assertEqual(result.state, "review")
        self.assertEqual(result.result, "完成")
        self.assertEqual(result.thread_id, "test-thread")

    def test_exit_zero_without_completed_is_failure(self):
        self.assertEqual(self.execute("import sys; sys.stdin.read()").state, "failed")

    def test_invalid_json_does_not_escape_to_logs(self):
        result = self.execute("import sys; sys.stdin.read(); print('PRIVATE_CONTENT')")
        self.assertEqual(result.state, "failed")
        self.assertNotIn("PRIVATE_CONTENT", result.error)

    def test_failed_event_even_with_success_exit(self):
        result = self.execute("import sys,json; sys.stdin.read(); print(json.dumps({'type':'turn.failed','error':{'message':'PRIVATE_CONTENT'}}))")
        self.assertEqual(result.state, "failed")
        self.assertNotIn("PRIVATE_CONTENT", result.error)

    def test_large_stderr_and_prompt_do_not_deadlock(self):
        result = self.execute('''
import sys
sys.stderr.write('private'*100000)
sys.stderr.flush()
sys.stdin.read()
''', prompt="任务" * 20000)
        self.assertEqual(result.state, "failed")

    def test_timeout_kills_child(self):
        started = time.monotonic()
        result = self.execute("import time; time.sleep(10)", timeout=0.2)
        self.assertIn("超时", result.error)
        self.assertLess(time.monotonic() - started, 4)

    def test_cancel_before_start(self):
        self.cancel.set()
        result = self.execute("raise RuntimeError('must not run')")
        self.assertEqual(result.state, "cancelled")

    def test_cancel_during_run(self):
        timer = threading.Timer(0.2, self.cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.execute("import time; time.sleep(10)")
        self.assertEqual(result.state, "cancelled")

    def test_invalid_sandbox_rejected(self):
        self.assertIn("danger-full-access", CodexExecutor().argv(Request(self.cwd, "测试", "", sandbox="danger-full-access", account_home=self.cwd)))
        with self.assertRaises(ValueError):
            CodexExecutor().argv(Request(self.cwd, "测试", "", sandbox="invalid", account_home=self.cwd))

    def test_resume_uses_explicit_uuid_and_rejects_changed_thread(self):
        session_id = str(__import__("uuid").uuid4())
        args = CodexExecutor().argv(Request(self.cwd, "测试", "", account_home=self.cwd, resume_thread_id=session_id))
        self.assertEqual(["resume", session_id, "-"], args[-3:])
        self.assertNotIn("--last", args)
        script = Path(self.cwd) / "resume_cli.py"
        script.write_text("import json,sys\nsys.stdin.read()\nprint(json.dumps({'type':'thread.started','thread_id':'" + str(__import__("uuid").uuid4()) + "'}),flush=True)\nprint(json.dumps({'type':'turn.completed'}),flush=True)\n", encoding="utf-8")
        executor = CodexExecutor((sys.executable, str(script)))
        result = executor.execute(Request(self.cwd, "测试", "", account_home=self.cwd, resume_thread_id=session_id), self.cancel, lambda *_: None)
        self.assertEqual("failed", result.state)
        self.assertIn("不一致", result.error)

    def test_run_marker_is_stdin_only_and_does_not_change_cli_arguments(self):
        run_id = str(__import__("uuid").uuid4())
        script = Path(self.cwd) / "marker_cli.py"
        script.write_text(
            "import json,sys\nmessage=sys.stdin.read()\n"
            f"assert '[工作台执行 {run_id}]' in message\n"
            f"assert {run_id!r} not in sys.argv\n"
            "for event in [{'type':'thread.started','thread_id':'marker-thread'},"
            "{'type':'item.completed','item':{'type':'agent_message','text':'完成'}},{'type':'turn.completed'}]: "
            "print(json.dumps(event),flush=True)\n", encoding="utf-8")
        executor = CodexExecutor((sys.executable, str(script)))
        result = executor.execute(Request(self.cwd, "测试", "标记", account_home=self.cwd, run_id=run_id), self.cancel, lambda *_: None)
        self.assertEqual("review", result.state)
        with self.assertRaises(ValueError):
            executor.argv(Request(self.cwd, "测试", "", account_home=self.cwd, run_id="not-a-uuid"))

    def test_model_argument_injection_rejected(self):
        with self.assertRaises(ValueError):
            CodexExecutor().argv(Request(self.cwd, "测试", "", model="--dangerously-bypass-approvals-and-sandbox", account_home=self.cwd))

    def test_missing_account_is_rejected_before_launch(self):
        with self.assertRaisesRegex(ValueError, "明确绑定执行账户"):
            CodexExecutor().argv(Request(self.cwd, "测试", ""))

    def test_config_is_per_invocation(self):
        args = CodexExecutor().argv(Request(self.cwd, "测试", "", model="chosen-model", effort="high", concurrency=4, account_home=self.cwd))
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIn("agents.max_concurrent_threads_per_session=4", args)
        self.assertIn('approval_policy="never"', args)
        self.assertNotIn("--approve-for-me", args)

    def test_current_account_omits_isolated_file_auth_and_requires_official_home(self):
        request = Request(self.cwd, "测试", "", account_home=self.cwd, use_current_account=True)
        args = CodexExecutor().argv(request)
        self.assertNotIn('cli_auth_credentials_store="file"', args)
        self.assertNotIn('forced_login_method="chatgpt"', args)
        script = Path(self.cwd) / "current_cli.py"
        script.write_text("""import json
for event in [
 {'type':'thread.started','thread_id':'current-thread'},
 {'type':'item.completed','item':{'type':'agent_message','text':'完成'}},
 {'type':'turn.completed'}]:
 print(json.dumps(event), flush=True)
""", encoding="utf-8")
        executor = CodexExecutor((sys.executable, str(script)))
        with mock.patch("codex_workbench.account_runtime.current_account_home", return_value=str(Path(self.cwd).resolve())):
            result = executor.execute(request, self.cancel, lambda *_: None)
        self.assertEqual("review", result.state)
        with mock.patch("codex_workbench.account_runtime.current_account_home", return_value=str(Path(self.cwd).resolve())):
            mismatched = Request(self.cwd, "测试", "", account_home=str(Path(self.cwd) / "other"), use_current_account=True)
            with self.assertRaisesRegex(ValueError, "当前账户"):
                executor.execute(mismatched, self.cancel, lambda *_: None)

    def test_parent_death_stops_orphan_execution(self):
        script = Path(self.cwd) / "slow_cli.py"
        script.write_text("import sys,time,pathlib\nsys.stdin.read()\npathlib.Path('started').touch()\ntime.sleep(1.5)\npathlib.Path('unwanted').touch()\n")
        driver = Path(self.cwd) / "driver.py"
        driver.write_text("from codex_workbench.executor import *\nimport sys,threading\nCodexExecutor((sys.executable,sys.argv[1])).execute(Request(sys.argv[2],'test','test',account_home=sys.argv[2]),threading.Event(),lambda *args:None)\n")
        package_root = str(Path(__file__).resolve().parents[1] / "src")
        parent = subprocess.Popen([sys.executable, str(driver), str(script), self.cwd],
                                  env={**os.environ, "PYTHONPATH": package_root})
        try:
            deadline = time.monotonic() + 3
            while not (Path(self.cwd) / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((Path(self.cwd) / "started").exists())
            parent.kill()
            parent.wait(3)
            time.sleep(1.8)
            self.assertFalse((Path(self.cwd) / "unwanted").exists())
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait()


if __name__ == "__main__":
    unittest.main()
