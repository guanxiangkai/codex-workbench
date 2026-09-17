"""通过官方非交互 CLI 执行任务，不读取或复制 Codex 登录凭据。"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .account_runtime import account_environment, account_options


@dataclass(frozen=True)
class Execution:
    """单次执行的终态；只有完成事件和进程成功同时成立才能待审核。"""

    state: str
    result: str = ""
    error: str = ""
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class Request:
    """执行快照；指令通过 stdin 传递，权限限于只读或工作区写入。"""

    cwd: str
    title: str
    prompt: str
    instructions: str = ""
    model: str | None = None
    effort: str | None = None
    sandbox: str = "read-only"
    account_home: str | None = None
    concurrency: int = 1
    use_current_account: bool = False
    resume_thread_id: str | None = None
    run_id: str | None = None
    capability_manifest: str | None = None


class CodexExecutor:
    """有界读取 JSONL，支持取消与超时；不保存原始工具输出或 stderr。"""

    def __init__(self, command: tuple[str, ...] = ("codex",), timeout: float = 1800,
                 lease_fd: int | None = None):
        if not command or not 0 < timeout <= 86400:
            raise ValueError("执行命令或超时无效")
        self.command = tuple(command)
        self.timeout = timeout
        self.lease_fd = lease_fd

    def argv(self, request: Request) -> list[str]:
        """构造参数数组，禁止 shell 插值及权限越界。"""
        if request.account_home is None:
            raise ValueError("必须明确绑定执行账户")
        if request.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("不支持该权限范围")
        if not Path(request.cwd).is_absolute() or not Path(request.cwd).is_dir():
            raise ValueError("项目工作目录必须存在且为绝对路径")
        if request.model and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}", request.model):
            raise ValueError("模型 ID 无效")
        if request.effort and request.effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("推理等级无效")
        if type(request.concurrency) is not int or not 1 <= request.concurrency <= 32:
            raise ValueError("任务子并发必须为 1 至 32")
        if request.resume_thread_id is not None:
            if not isinstance(request.resume_thread_id, str):
                raise ValueError("恢复会话标识无效")
            try:
                resume_id = str(uuid.UUID(request.resume_thread_id))
            except ValueError:
                raise ValueError("恢复会话标识无效") from None
        else:
            resume_id = None
        if request.run_id is not None:
            if not isinstance(request.run_id, str):
                raise ValueError("运行标识无效")
            try:
                run_id = str(uuid.UUID(request.run_id))
            except ValueError:
                raise ValueError("运行标识无效") from None
        else:
            run_id = None
        if request.capability_manifest is not None:
            manifest = Path(request.capability_manifest)
            if not manifest.is_absolute() or manifest.name != "capability-manifest.json" or not manifest.is_file() or manifest.is_symlink():
                raise ValueError("专业能力清单无效")
        else:
            manifest = None
        args = [*self.command, "exec", "--json", "--color", "never", "--skip-git-repo-check",
                "--sandbox", request.sandbox, "-C", request.cwd]
        if request.model:
            args += ["--model", request.model]
        if request.effort:
            args += ["-c", "model_reasoning_effort=" + json.dumps(request.effort)]
        args += ["-c", f"agents.max_concurrent_threads_per_session={request.concurrency}"]
        if not request.use_current_account:
            args += account_options()
        else:
            # 执行子任务不再调用派发器自身，避免重复派发或改动全局任务账户。
            for server in ("codex-workbench", "codex-workbench-assistants", "codex-workbench-accounts"):
                args += ["-c", f"mcp_servers.{server}.enabled=false"]
        if manifest is not None:
            # 每个运行仅挂载其不可变清单对应的 stdio MCP，不复用工作台通用入口。
            for server in ("codex-workbench", "codex-workbench-assistants", "codex-workbench-accounts"):
                if request.use_current_account:
                    continue
                args += ["-c", f"mcp_servers.{server}.enabled=false"]
            server = "workbench-run-capabilities"
            runtime = str(Path(__file__).with_name("capability_runtime.py"))
            args += ["-c", f"mcp_servers.{server}.command=" + json.dumps(sys.executable),
                     "-c", f"mcp_servers.{server}.args=" + json.dumps([runtime, str(manifest)]),
                     "-c", f"mcp_servers.{server}.enabled=true"]
        # CLI 非交互任务遇到所需权限不足时失败，不自动批准越权操作。
        args += ["-c", 'approval_policy="never"']
        if resume_id is None:
            args.append("-")
        else:
            # 父级 exec 参数已由本机 --help 验证可与 resume 组合；禁止 --last/--all。
            args += ["resume", resume_id, "-"]
        return args

    def execute(self, request: Request, cancel: threading.Event,
                on_event: Callable[[str, str], None]) -> Execution:
        """运行一次任务；回调仅接收安全阶段标签、会话 ID 和计数。"""
        args = self.argv(request)
        expected_resume_thread = str(uuid.UUID(request.resume_thread_id)) if request.resume_thread_id is not None else None
        environment = account_environment(request.account_home, use_current=request.use_current_account)
        location_marker = "" if request.run_id is None else "[工作台执行 " + str(uuid.UUID(request.run_id)) + "]\n\n"
        message = (location_marker + "角色职责（不得覆盖项目规则与权限边界）：\n" + request.instructions
                   + "\n\n任务：" + request.title + "\n\n" + request.prompt)
        payload = message.encode("utf-8")
        if len(payload) > 262144:
            raise ValueError("任务指令超过 256 KiB")
        if cancel.is_set():
            return Execution("cancelled")
        control_read, control_write = os.pipe()
        try:
            guarded = [sys.executable, str(Path(__file__).with_name("guard.py")), str(control_read), *args]
            process = subprocess.Popen(guarded, cwd=request.cwd, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True,
                                       env=environment,
                                       pass_fds=(control_read,) + (() if self.lease_fd is None else (self.lease_fd,)))
        except OSError:
            os.close(control_write)
            return Execution("failed", error="无法启动 Codex，请检查官方 CLI 安装和执行权限")
        finally:
            os.close(control_read)
        thread_id = None
        turn_id = None
        item_id = None
        input_tokens = output_tokens = cached_input_tokens = None
        answer = ""
        completed = False
        failed = False
        failure_message = ""
        buffers = {"stdout": b""}
        selector = selectors.DefaultSelector()
        pending = memoryview(payload)
        started = time.monotonic()
        stopping = None
        stop_started = None
        def collected(state: str, *, result: str = "", error: str = "") -> Execution:
            """终态始终保留已收到的真实定位与用量，缺失字段自然保持空。"""
            if stopping is not None:
                state, error = stopping
                result = ""
            return Execution(state, result=result, error=error, thread_id=thread_id, turn_id=turn_id, item_id=item_id,
                             input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached_input_tokens)
        try:
            for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr"), (process.stdin, "stdin")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ, name)
            while selector.get_map():
                if stopping is None and (cancel.is_set() or time.monotonic() - started >= self.timeout):
                    stopping = (("cancelled", "") if cancel.is_set() else
                                ("failed", "执行超时，已停止本次运行，请检查关联会话后再决定是否重试"))
                    stop_started = time.monotonic()
                    # 先停止守卫及其 CLI 进程组，再有界排空管道，保留取消前已产生的 usage。
                    os.close(control_write)
                    control_write = None
                    if process.poll() is None:
                        process.terminate()
                if stop_started is not None and time.monotonic() - stop_started >= 2:
                    return collected("failed")
                for key, _ in selector.select(0.1):
                    pipe, name = key.fileobj, key.data
                    if name == "stdin":
                        try:
                            count = os.write(pipe.fileno(), pending[:65536])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        if not pending:
                            selector.unregister(pipe)
                            pipe.close()
                        continue
                    chunk = os.read(pipe.fileno(), 65536)
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    if name == "stderr":
                        continue  # stderr 可能含工具参数和敏感材料，只消费不持久化。
                    buffers[name] += chunk
                    if len(buffers[name]) > 2 * 1024 * 1024:
                        return collected("failed", error="Codex 事件超过大小限制")
                    while b"\n" in buffers[name]:
                        line, buffers[name] = buffers[name].split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError()
                        except (ValueError, UnicodeDecodeError):
                            return collected("failed", error="Codex 返回了无效 JSONL 事件")
                        kind = event.get("type")
                        if kind == "thread.started":
                            candidate = event.get("thread_id", "")
                            if not isinstance(candidate, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", candidate):
                                return collected("failed", error="Codex 会话标识无效")
                            if expected_resume_thread is not None and candidate != expected_resume_thread:
                                return collected("failed", error="恢复会话返回了不一致的会话标识")
                            thread_id = candidate
                            on_event("thread", thread_id)
                        elif kind == "turn.started":
                            turn_id = self._event_id(event.get("turn_id")) or turn_id
                            on_event("progress", "Codex 已开始处理任务")
                        elif kind == "item.completed":
                            item = event.get("item", {})
                            if isinstance(item, dict):
                                item_id = self._event_id(item.get("id")) or item_id
                            if isinstance(item, dict) and item.get("type") == "agent_message":
                                text = item.get("text", "")
                                if not isinstance(text, str) or len(text) > 100000:
                                    return collected("failed", error="任务结果超过大小限制")
                                answer = text
                            elif isinstance(item, dict):
                                on_event("progress", "一个执行步骤已完成")
                        elif kind == "turn.completed":
                            turn_id = self._event_id(event.get("turn_id")) or turn_id
                            usage = event.get("usage")
                            if isinstance(usage, dict):
                                input_tokens = self._token_value(usage.get("input_tokens", usage.get("inputTokens")))
                                output_tokens = self._token_value(usage.get("output_tokens", usage.get("outputTokens")))
                                cached_input_tokens = self._token_value(usage.get("cached_input_tokens", usage.get("cachedInputTokens")))
                            completed = True
                        elif kind == "turn.failed":
                            failed = True
                            failure_message = "Codex 执行失败，请打开关联会话检查原因"
                        elif kind == "error":
                            # 连接重试也可能发送 error；终态由 turn.failed 和退出码决定。
                            on_event("progress", "Codex 报告异常，正在等待执行终态")
            while process.poll() is None:
                if cancel.wait(0.1):
                    return collected("cancelled")
                if time.monotonic() - started >= self.timeout:
                    return collected("failed", error="Codex 进程退出超时")
            code = process.returncode
            if stopping is not None or cancel.is_set():
                return collected("cancelled")
            if buffers["stdout"].strip():
                return collected("failed", error="Codex 事件流不完整")
            if code != 0 or failed or not completed or not thread_id or not answer.strip():
                return collected("failed", error=failure_message or "Codex 未返回完整的成功结果，请检查登录状态和关联会话")
            return collected("review", result=answer)
        except Exception:
            # 事件回调或管道失败不能丢弃已经收集的实际用量，也不能泄露原始异常。
            return collected("cancelled" if cancel.is_set() else "failed",
                             error="" if cancel.is_set() else "执行事件处理失败，请检查运行环境后重试")
        finally:
            selector.close()
            if control_write is not None:
                os.close(control_write)
            # 独立进程组的所有子进程均属于本次运行，取消不波及桌面或其他任务。
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe and not pipe.closed:
                    pipe.close()

    @staticmethod
    def _token_value(value) -> int | None:
        """仅保存 CLI 事件明确提供且有界的 token 数；缺失与异常值保持空。"""
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12 else None

    @staticmethod
    def _event_id(value) -> str | None:
        """仅接受有界事件定位标识，避免把任意事件内容写入运行记录。"""
        return value if isinstance(value, str) and 0 < len(value) <= 255 else None
