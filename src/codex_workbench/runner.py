"""任务执行协调；持久状态由 Store 独占，进程只持有当前执行的取消句柄。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .executor import CodexExecutor, Request
from .store import Store
from .capability_manifest import revoke_capability_manifest


@dataclass
class Active:
    """当前进程拥有的运行句柄，不作为持久状态来源。"""

    task_id: str
    cancel: threading.Event
    thread: threading.Thread
    finalizing: bool = False


class Runner:
    """显式执行与取消入口，限制总并发；不在初始化时自动派发任务。"""

    def __init__(self, store: Store, executor: CodexExecutor | None = None, concurrency: int = 2, prepare=None, native_attached=None):
        if not 1 <= concurrency <= 8:
            raise ValueError("总并发必须为 1 至 8")
        self.store = store
        self.executor = executor or CodexExecutor()
        self.concurrency = concurrency
        self._lock = threading.Lock()
        self._active: dict[str, Active] = {}
        self._closed = False
        self.prepare = prepare
        self.native_attached = native_attached

    def start(self, task_id: str) -> dict:
        """原子领取待执行任务；同任务或同角色并发冲突由数据库拒绝。"""
        with self._lock:
            if self._closed:
                raise ValueError("工作台正在关闭")
            if len(self._active) >= self.concurrency:
                raise ValueError("已达到工作台总并发上限")
            run = self.store.claim(task_id)
            if self.prepare is not None:
                try:
                    self.prepare(run)
                except Exception:
                    if run.get("capability_manifest"):
                        revoke_capability_manifest(run["capability_manifest"])
                    self.store.finish(run["id"], "failed", error="任务资源准备失败，请检查规则或模板选择")
                    raise
            cancel = threading.Event()
            thread = threading.Thread(target=self._execute, args=(run, cancel), daemon=True)
            self._active[run["id"]] = Active(task_id, cancel, thread)
            try:
                thread.start()
            except RuntimeError:
                if run.get("capability_manifest"):
                    revoke_capability_manifest(run["capability_manifest"])
                del self._active[run["id"]]
                self.store.finish(run["id"], "failed", error="无法启动执行线程")
                raise
            return run

    def cancel(self, run_id: str) -> None:
        """仅取消本进程拥有的执行，不接受任意系统 PID。"""
        with self._lock:
            active = self._active.get(run_id)
            if not active or active.finalizing:
                raise ValueError("运行已结束或不属于当前工作台进程")
            active.cancel.set()

    def close(self) -> None:
        """停止接受任务并取消活动执行；等待进程和数据库写入结束。"""
        with self._lock:
            self._closed = True
            active = list(self._active.values())
            for item in active:
                item.cancel.set()
        for item in active:
            item.thread.join(timeout=5)
        if any(item.thread.is_alive() for item in active):
            raise RuntimeError("仍有执行未结束，不能关闭任务数据库")

    def _execute(self, run: dict, cancel: threading.Event) -> None:
        run_id = run["id"]
        task, project, agent = run["task"], run["project"], run["agent"]
        account = run.get("execution_account")
        execution = run["execution"]
        request = Request(cwd=project["cwd"], title=task["title"], prompt=task["prompt"],
                          instructions=agent["instructions"], model=execution["model"],
                          effort=execution["effort"], sandbox=execution["sandbox"], concurrency=execution["concurrency"],
                          account_home=account["codex_home"] if account else None,
                          use_current_account=bool(account and account.get("kind") == "current"),
                          resume_thread_id=run.get("session", {}).get("native_thread_id"), run_id=run_id, capability_manifest=run.get("capability_manifest"))
        event_count = 0
        def on_event(kind: str, message: str) -> None:
            nonlocal event_count
            if kind == "thread":
                self.store.attach_thread(run_id, message)
                if self.native_attached is not None:
                    try:
                        self.native_attached(run, message)
                    except Exception:
                        self.store.append_event(run_id, "notice", "原生会话名称同步暂未完成；执行继续")
            elif event_count < 199:
                self.store.append_event(run_id, kind, message)
                event_count += 1
            elif event_count == 199:
                self.store.append_event(run_id, "notice", "进度记录已达上限；执行继续，最终结果仍会保存")
                event_count += 1
        started = time.monotonic()
        metrics_recorded = False
        try:
            outcome = self.executor.execute(request, cancel, on_event)
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            self.store.record_run_metrics(run_id, duration_ms=duration_ms, turn_id=outcome.turn_id, item_id=outcome.item_id,
                                          input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                                          cached_input_tokens=outcome.cached_input_tokens)
            metrics_recorded = True
            with self._lock:
                self._active[run_id].finalizing = True
                cancelled = cancel.is_set()
            self.store.finish(run_id, "cancelled" if cancelled else outcome.state,
                              result="" if cancelled else outcome.result,
                              error="" if cancelled else outcome.error)
        except Exception:
            # 异常内容可能含路径、SQL 或执行输入；原始异常不得写入公开事件。
            try:
                if not metrics_recorded:
                    self.store.record_run_metrics(run_id, duration_ms=max(0, int((time.monotonic() - started) * 1000)))
            except Exception:
                pass
            with self._lock:
                self._active[run_id].finalizing = True
                cancelled = cancel.is_set()
            self.store.finish(run_id, "cancelled" if cancelled else "failed",
                              error="" if cancelled else "执行协调失败，请检查运行环境后重试")
        finally:
            try:
                if run.get("capability_manifest"):
                    revoke_capability_manifest(run["capability_manifest"])
            finally:
                with self._lock:
                    self._active.pop(run_id, None)
