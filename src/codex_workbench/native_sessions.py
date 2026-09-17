"""原生 Codex 线程的公开 RPC 客户端，只处理名称和运行状态。"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .account_runtime import account_environment, account_options
from .catalog import VERSION


_NAME_TOKEN_KEY = os.urandom(32)


def name_token(identity: str, name: str | None) -> str:
    """对身份和原始名称生成进程内 HMAC；区分空值，不暴露可离线猜测的标题摘要。

    进程重启会使旧令牌失效，需刷新页面；它仅校验已观察名称，不是原生 CAS。
    """
    payload = json.dumps([identity, name], ensure_ascii=True, separators=(",", ":")).encode()
    return "n1:" + hmac.new(_NAME_TOKEN_KEY, payload, hashlib.sha256).hexdigest()


def name_token_matches(expected: object, identity: str, name: str | None) -> bool:
    """校验同一身份的精确原始名称；缺失或格式无效的令牌均不能作为写入前提。"""
    return (isinstance(expected, str) and expected.isascii()
            and hmac.compare_digest(expected, name_token(identity, name)))


class NativeSessionError(ValueError):
    """原生线程 RPC 的可处理错误，不包含认证材料或服务端原始响应。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeSessionClient:
    """使用指定受管账户目录调用原生线程名称和状态的最小公开 RPC 集合。"""

    METHODS = frozenset({"thread/read", "thread/name/set"})
    _STATUSES = frozenset({"active", "idle", "notLoaded", "systemError"})

    def __init__(self, codex: str, account_home: str, lease_fd: int | None = None, timeout: float = 25,
                 *, use_current_account: bool = False) -> None:
        self.timeout = timeout
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        self._pending: dict[int, queue.Queue[dict[str, Any] | None]] = {}
        self._lock = threading.RLock()
        self._next = 0
        self._closed = False
        read_fd, self._control = os.pipe()
        command = [sys.executable, str(Path(__file__).with_name("guard.py")), str(read_fd), codex,
                   "app-server", "--listen", "stdio://", *([] if use_current_account else account_options())]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=account_environment(account_home, use_current=use_current_account),
                pass_fds=(read_fd,) + (() if lease_fd is None else (lease_fd,)),
                start_new_session=True,
            )
        except (OSError, ValueError):
            os.close(self._control)
            raise NativeSessionError("unavailable", "无法启动官方原生会话接口，请检查 Codex CLI 与账户目录") from None
        finally:
            os.close(read_fd)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {"clientInfo": {"name": "codex_workbench", "version": VERSION}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def read(self, thread_id: str) -> dict[str, Any]:
        """不请求轮次历史，仅保留名称和状态；官方摘要仍可能含 preview，此处丢弃。"""
        self._text(thread_id, "Codex 会话标识", 255)
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise NativeSessionError("protocol", "官方原生会话接口返回无效线程")
        status = thread.get("status")
        if not isinstance(status, dict) or status.get("type") not in self._STATUSES:
            raise NativeSessionError("protocol", "官方原生会话接口未返回可识别状态")
        if "name" not in thread:
            raise NativeSessionError("protocol", "官方原生会话接口未返回名称字段")
        name = thread["name"]
        if name is not None and not isinstance(name, str):
            raise NativeSessionError("protocol", "官方原生会话接口返回无效名称")
        return {"id": thread_id, "name": name, "status": dict(status)}

    def set_name(self, thread_id: str, name: str) -> dict[str, Any]:
        """写入名称并读回；公开接口无 CAS 参数，读回不能消除读写间竞态。"""
        self._text(thread_id, "Codex 会话标识", 255)
        self._text(name, "会话标题", 300, allow_empty=True)
        self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return self.read(thread_id)

    def resume_gate(self, thread_id: str) -> dict[str, Any]:
        """拒绝当前连接已观察到 active 的线程，并返回其余状态的可观测性边界。"""
        thread = self.read(thread_id)
        status = thread["status"]["type"]
        if status == "active":
            raise NativeSessionError("thread_active", "原生会话正在执行，不能从工作台恢复执行")
        observability = {
            "idle": "confirmed_idle",
            "notLoaded": "not_loaded",
            "systemError": "status_error",
        }[status]
        return {**thread, "resume_observability": observability}

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """调用固定白名单；不暴露通用 RPC 执行入口。"""
        if method not in self.METHODS:
            raise NativeSessionError("forbidden", "不支持该原生会话操作")
        return self._request(method, params or {})

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        channel: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise NativeSessionError("closed", "原生会话连接已关闭")
            self._next += 1
            request_id = self._next
            self._pending[request_id] = channel
            self._send({"method": method, "params": params, "id": request_id})
        try:
            response = channel.get(timeout=self.timeout)
            if response is None:
                raise NativeSessionError("unavailable", "官方原生会话接口已断开")
            if "error" in response:
                raise NativeSessionError("failed", "官方原生会话接口未完成请求，请稍后重试")
            return response.get("result")
        except queue.Empty:
            raise NativeSessionError("timeout", "官方原生会话接口响应超时") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _send(self, message: dict[str, Any]) -> None:
        with self._lock:
            try:
                self.process.stdin.write((json.dumps(message) + "\n").encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise NativeSessionError("unavailable", "原生会话接口不可写") from None

    def _read(self) -> None:
        try:
            while True:
                line = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                if not line or len(line) > 2 * 1024 * 1024:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                if "method" in message and "id" in message:
                    self._send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                elif "id" in message:
                    with self._lock:
                        target = self._pending.get(message["id"])
                    if target is not None:
                        target.put_nowait(message)
                elif message.get("method") == "thread/status/changed":
                    try:
                        self.events.put_nowait(message)
                    except queue.Full:
                        pass
        except (OSError, ValueError, queue.Full, RecursionError):
            pass
        finally:
            with self._lock:
                for target in self._pending.values():
                    try:
                        target.put_nowait(None)
                    except queue.Full:
                        pass

    def close(self) -> None:
        """关闭本次 RPC 子进程及其守卫控制管道。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._control)
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe and not pipe.closed:
                pipe.close()
        self._reader.join(timeout=1)

    def __enter__(self) -> "NativeSessionClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _text(value: Any, label: str, maximum: int, allow_empty: bool = False) -> None:
        if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
            raise NativeSessionError("validation", f"{label}无效")
