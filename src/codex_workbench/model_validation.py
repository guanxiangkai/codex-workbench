"""模型实际验证的单线程后台工作者。"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from .model_registry import ModelRegistry


class ModelValidationWorker:
    """领取到期模型并调用注入的 probe；数据库租约避免多个实例重复请求。"""

    def __init__(self, registry: ModelRegistry, probe: Callable[[dict[str, Any], threading.Event], dict[str, Any]]) -> None:
        """创建未启动的工作者；probe 必须由运行时提供且不得持有工作台主锁。"""
        self.registry, self.probe = registry, probe
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._worker_id = "model-validation-" + str(uuid.uuid4())

    def start(self) -> None:
        """启动唯一后台线程；重复调用不会额外创建验证循环。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name="model-validation", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        """唤醒等待中的工作者，使新建或手动排队模型尽快验证。"""
        self._wake.set()

    def close(self) -> bool:
        """请求停止并有界等待；返回线程是否已经停止，供运行时报告无法取消的 probe。"""
        self._cancel.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=31)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while not self._cancel.is_set():
            try:
                claimed = self.registry.claim_due(self._worker_id)
            except Exception:
                self._wait_after_error()
                continue
            if claimed is None:
                self._wake.wait(timeout=1)
                self._wake.clear()
                continue
            try:
                result = self.probe(claimed, self._cancel)
                if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                    result = {"success": False, "code": "invalid_probe_result", "message": "验证器返回无效结果"}
            except Exception:
                result = {"success": False, "code": "probe_error", "message": "模型验证请求失败"}
            if self._cancel.is_set():
                continue
            try:
                self.registry.finish(claimed["id"], claimed["version"], claimed["lease_token"], result["success"],
                                     None if result["success"] else result.get("code"),
                                     None if result["success"] else result.get("message"))
            except Exception:
                self._wait_after_error()

    def _wait_after_error(self) -> None:
        """SQLite 暂时不可用时保留线程并有界等待，关闭请求可立即中断。"""
        self._wake.wait(timeout=1)
        self._wake.clear()
