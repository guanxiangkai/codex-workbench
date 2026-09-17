"""官方 Codex 账户 RPC 的受限适配器；只调用登录和只读账户方法。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .account_runtime import account_environment, account_options


class RpcError(ValueError):
    """可向界面报告的脱敏账户接口错误。"""


class AccountRpc:
    """一个独立账户目录对应一个短期 RPC 会话，不接受任意方法调用。"""

    METHODS = {"account/read", "account/rateLimits/read", "account/login/start", "account/login/cancel", "model/list", "config/read"}
    CURRENT_METHODS = {"account/read", "account/rateLimits/read", "model/list", "config/read"}

    def __init__(self, codex: str, account_home: str, lease_fd: int | None = None, timeout: float = 25, *, current: bool = False):
        self.timeout = timeout
        self.current = current
        self.events: queue.Queue[dict] = queue.Queue(maxsize=100)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.RLock()
        self._next = 0
        self._closed = False
        read_fd, self._control = os.pipe()
        command = [sys.executable, str(Path(__file__).with_name("guard.py")), str(read_fd), codex,
                   "app-server", "--listen", "stdio://", *([] if current else account_options())]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=account_environment(account_home, use_current=current),
                                            pass_fds=(read_fd,) + (() if lease_fd is None else (lease_fd,)),
                                            start_new_session=True)
        except (OSError, ValueError):
            os.close(self._control)
            raise RpcError("无法启动官方账户接口，请检查 Codex CLI 与账户目录") from None
        finally:
            os.close(read_fd)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {"clientInfo": {"name": "codex_workbench", "version": "0.2.0"}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def request(self, method: str, params: dict | None = None) -> Any:
        """调用白名单方法；不会发送执行命令、原始令牌或权限批准。"""
        if method not in (self.CURRENT_METHODS if self.current else self.METHODS):
            raise RpcError("不支持该账户操作")
        return self._request(method, params or {})

    def _request(self, method: str, params: dict) -> Any:
        channel: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise RpcError("账户连接已关闭")
            self._next += 1
            request_id = self._next
            self._pending[request_id] = channel
            self._send({"method": method, "params": params, "id": request_id})
        try:
            response = channel.get(timeout=self.timeout)
            if response is None:
                raise RpcError("官方账户接口已断开")
            if "error" in response:
                raise RpcError("官方账户接口未完成请求，请检查登录状态或稍后重试")
            return response.get("result")
        except queue.Empty:
            raise RpcError("官方账户接口响应超时") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _send(self, message: dict) -> None:
        with self._lock:
            try:
                self.process.stdin.write((json.dumps(message) + "\n").encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise RpcError("账户接口不可写") from None

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
                    # 账户适配器没有工具批准能力，拒绝任何服务端交互请求。
                    self._send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                elif "id" in message:
                    with self._lock:
                        target = self._pending.get(message["id"])
                    if target:
                        target.put_nowait(message)
                elif message.get("method") in {"account/login/completed", "account/updated"}:
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
        """关闭控制管道，让守卫清理此账户会话及子进程。"""
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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
