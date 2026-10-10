"""本机公开视图快照与单进程刷新调度。

页面只读取这个存储；采集器是唯一允许调用来源适配器的路径。文件不包含
凭据、认证材料或配置详情，并且按账户环境摘要隔离。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor


DEFAULT_SCHEDULE = {
    "version": 1,
    "provider_timers_enabled": False,
    "active_window_seconds": 900,
    "provider_tasks": {
        "codex": {"interval_seconds": 900, "phase_seconds": 0},
        "bigmodel": {"interval_seconds": 900, "phase_seconds": 300},
        "minimax": {"interval_seconds": 900, "phase_seconds": 600},
        "manual_reset": {"interval_seconds": 3600, "phase_seconds": 720},
    },
    "views": {
        "accounts": {"active_seconds": 60, "idle_seconds": 300, "profile_seconds": 86400},
        "other_accounts": {"active_seconds": 300, "idle_seconds": 1800},
        "config": {"active_seconds": 300, "idle_seconds": 1800},
        "models": {"active_seconds": 600, "idle_seconds": 3600},
        "agents": {"active_seconds": 1800, "idle_seconds": 21600},
        "knowledge": {"active_seconds": 600, "idle_seconds": 3600},
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_schedule(path: Path | None) -> dict:
    """读取公开刷新规则；损坏或缺字段时安全回退默认值。"""
    result = deepcopy(DEFAULT_SCHEDULE)
    if path is None:
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return result
    if not isinstance(value, dict):
        return result
    if isinstance(value.get("active_window_seconds"), int) and value["active_window_seconds"] > 0:
        result["active_window_seconds"] = value["active_window_seconds"]
    if isinstance(value.get("provider_timers_enabled"), bool):
        result["provider_timers_enabled"] = value["provider_timers_enabled"]
    if isinstance(value.get("provider_tasks"), dict):
        for provider, rule in value["provider_tasks"].items():
            if provider not in result["provider_tasks"] or not isinstance(rule, dict):
                continue
            interval = rule.get("interval_seconds")
            phase = rule.get("phase_seconds")
            if isinstance(interval, int) and interval > 0:
                result["provider_tasks"][provider]["interval_seconds"] = interval
            if isinstance(phase, int) and phase >= 0:
                result["provider_tasks"][provider]["phase_seconds"] = phase
    if isinstance(value.get("views"), dict):
        for name, rule in value["views"].items():
            if name not in result["views"] or not isinstance(rule, dict):
                continue
            for key in ("active_seconds", "idle_seconds", "profile_seconds"):
                if key == "profile_seconds" and name != "accounts":continue
                if isinstance(rule.get(key), int) and rule[key] > 0:
                    result["views"][name][key] = rule[key]
    return result


class SnapshotStore:
    """持久化最后成功的公开快照，崩溃或失败均不清除旧值。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "snapshots.json"
        self.lock = threading.RLock()
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_symlink():
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get("contexts"), dict):
                allowed = set(DEFAULT_SCHEDULE["views"])
                self.entries = {
                    str(context): {
                        view: entry for view, entry in views.items()
                        if view in allowed and isinstance(entry, dict)
                        and isinstance(entry.get("data"), dict)
                        and isinstance(entry.get("updated_at"), str)
                    }
                    for context, views in value["contexts"].items()
                    if isinstance(views, dict)
                }
                self.entries = {context: views for context, views in self.entries.items() if views}
        except (OSError, ValueError, json.JSONDecodeError):
            self.entries = {}

    def get(self, context: str, view: str) -> dict | None:
        with self.lock:
            value = self.entries.get(context, {}).get(view)
            return deepcopy(value) if isinstance(value, dict) else None

    def put(self, context: str, view: str, data: dict) -> dict:
        entry = {"data": deepcopy(data), "updated_at": _now()}
        with self.lock:
            next_entries = deepcopy(self.entries)
            next_entries.setdefault(context, {})[view] = entry
            # 账户环境摘要只能是有限的近期历史，避免长期运行无限增长。
            while len(next_entries) > 8:
                next_entries.pop(next(iter(next_entries)))
            self._save(next_entries)
            self.entries = next_entries
        return deepcopy(entry)

    def _save(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("快照文件不能是符号链接")
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshots-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump({"version": 1, "contexts": entries}, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class SnapshotScheduler:
    """单一后台线程按页面活跃程度采集快照。"""

    def __init__(self, service, schedule_path: Path | None = None, clock=time.monotonic):
        self.service, self.schedule, self.clock = service, load_schedule(schedule_path), clock
        if hasattr(service.native, "profile_refresh_seconds"):
            service.native.profile_refresh_seconds = self.schedule["views"]["accounts"]["profile_seconds"]
        self.lock = threading.Condition()
        self.last_access: dict[str, float] = {}
        self.last_attempt: dict[tuple[str, str], float] = {}
        self.provider_last_attempt: dict[str, float] = {}
        self.provider_last_completed: dict[str, float] = {}
        wall = time.time()
        self.provider_last_slot: dict[str, int] = {
            provider: int((wall - rule['phase_seconds']) // rule['interval_seconds'])
            for provider, rule in self.schedule['provider_tasks'].items()}
        self.provider_errors: dict[str, str] = {}
        self.provider_states: dict[str, str] = {}
        self.provider_results: dict[str, dict] = {}
        self.provider_running: set[str] = set()
        self.provider_requested: set[str] = set()
        self.requested: set[tuple[str, str]] = set()
        self.running: set[tuple[str, str]] = set()
        self.closed = False
        self.thread: threading.Thread | None = None
        self.workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="workbench-collect")

    def start(self) -> None:
        with self.lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(target=self._run, name="workbench-snapshots", daemon=True)
            self.thread.start()

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.lock.notify_all()
        if self.thread is not None:
            self.thread.join(2)
        self.workers.shutdown(wait=False, cancel_futures=True)

    def provider_tasks(self) -> list[dict]:
        """Expose quota and manual-reset jobs for native automation polling.

        The scheduler owns cadence and de-duplication; the service supplies the
        provider-specific refresh callback. No credentials are returned here.
        """
        tasks = self.schedule.get('provider_tasks', {})
        wall = time.time()
        return [{'id': provider, 'interval_seconds': rule.get('interval_seconds', 900),
                 'phase_seconds': rule.get('phase_seconds', 0),
                 'last_attempt_at': (datetime.fromtimestamp(self.provider_last_attempt[provider], timezone.utc).isoformat()
                                     if provider in self.provider_last_attempt else None),
                 'last_completed_at': (datetime.fromtimestamp(self.provider_last_completed[provider], timezone.utc).isoformat()
                                       if provider in self.provider_last_completed else None),
                 'next_attempt_at': (datetime.fromtimestamp(self._next_provider_time(provider, wall), timezone.utc).isoformat()
                                     if self.schedule.get('provider_timers_enabled', False) else None),
                 'state': ('running' if provider in self.provider_running else 'queued' if provider in self.provider_requested
                           else self.provider_states.get(provider, 'idle')),
                 'result': deepcopy(self.provider_results.get(provider, {})),
                 'scheduler': ('workbench' if self.schedule.get('provider_timers_enabled', False)
                               else 'codex_automation'),
                 'queued': provider in self.provider_requested,
                 'error': self.provider_errors.get(provider),
                 'running': provider in self.provider_running}
                for provider, rule in tasks.items()]

    def request_provider(self, provider):
        if provider not in self.schedule['provider_tasks']:
            raise ValueError('未知额度供应商')
        with self.lock:
            if provider not in self.provider_running:
                self.provider_requested.add(provider)
            self.lock.notify_all()

    def touch(self, view: str, refresh: bool = False) -> None:
        if view not in self.schedule["views"]:
            return
        context=self.service.source_versions.context()
        with self.lock:
            self.last_access[view] = self.clock()
            key=(context,view)
            # 轮询页面不能让同一失败来源每两秒重新发起一次请求。
            if refresh or (not self.service.has_snapshot(view) and self.clock()-self.last_attempt.get(key,-float('inf')) >= 5):
                self.requested.add(key)
            self.lock.notify_all()

    def _interval(self, view: str, now: float) -> float:
        rule = self.schedule["views"][view]
        active = now - self.last_access.get(view, -float("inf")) <= self.schedule["active_window_seconds"]
        return rule["active_seconds"] if active else rule["idle_seconds"]

    def _due(self, context: str, view: str, now: float) -> bool:
        key=(context,view)
        return key not in self.running and (key in self.requested or now - self.last_attempt.get(key, -float("inf")) >= self._interval(view, now))

    def _run(self) -> None:
        while True:
            with self.lock:
                if self.closed:
                    return
                now = self.clock()
                try:
                    context=self.service.source_versions.context()
                except (ValueError, OSError):
                    self.lock.wait(5)
                    continue
                due = next((view for view in self.schedule["views"] if self._due(context, view, now)), None)
                provider = self._due_provider(time.time())
                if provider is not None:
                    self.provider_last_attempt[provider] = time.time()
                    self.provider_running.add(provider)
                    future = self.workers.submit(self._refresh_provider, provider)
                    future.add_done_callback(lambda _result, provider=provider: self._finished_provider(provider))
                    continue
                if due is None:
                    wait = min(max(0.1, self._interval(view, now) - (now - self.last_attempt.get((context,view), now))) for view in self.schedule["views"])
                    provider_wait = min(self._next_provider_time(p, time.time()) - time.time()
                                        for p in self.schedule['provider_tasks'])
                    self.lock.wait(max(0.1, min(wait, provider_wait, 30)))
                    continue
                key=(context,due)
                self.requested.discard(key)
                self.last_attempt[key] = now
                self.running.add(key)
            # 一个慢供应商不能阻塞配置、目录等其他采集。
            future=self.workers.submit(self.service.collect_snapshot,due)
            future.add_done_callback(lambda _result, key=key: self._finished(key))

    def _next_provider_time(self, provider: str, wall: float) -> float:
        rule = self.schedule.get('provider_tasks', {}).get(provider, {})
        interval = max(1, int(rule.get('interval_seconds', 900)))
        phase = int(rule.get('phase_seconds', 0)) % interval
        base = wall - (wall % interval) + phase
        return base if base > wall else base + interval

    def _due_provider(self, wall: float) -> str | None:
        for provider in sorted(self.provider_requested):
            if provider not in self.provider_running:
                self.provider_requested.discard(provider)
                return provider
        if not self.schedule.get('provider_timers_enabled', False):
            return None
        for provider, rule in self.schedule.get('provider_tasks', {}).items():
            if provider in self.provider_running:
                continue
            interval = max(1, int(rule.get('interval_seconds', 900)))
            phase = int(rule.get('phase_seconds', 0)) % interval
            slot = int((wall - phase) // interval)
            if self.provider_last_slot.get(provider, slot) < slot:
                self.provider_last_slot[provider] = slot
                return provider
        return None

    def _refresh_provider(self, provider: str) -> None:
        callback = getattr(self.service, 'refresh_provider_usage', None)
        if callback is not None:
            try:
                result = callback(provider)
                self.provider_results[provider] = result if isinstance(result, dict) else {}
                self.provider_states[provider] = (result.get('state') if isinstance(result, dict) and result.get('state') else 'ready')
                self.provider_errors.pop(provider, None)
            except Exception as error:
                # Keep the scheduler alive and expose a bounded diagnostic;
                # credentials and provider response bodies never enter state.
                self.provider_errors[provider] = type(error).__name__
                self.provider_states[provider] = 'error'
            finally:
                self.provider_last_completed[provider] = time.time()

    def _finished_provider(self, provider: str) -> None:
        with self.lock:
            self.provider_running.discard(provider)
            self.lock.notify_all()

    def _finished(self, key):
        with self.lock:
            self.running.discard(key)
            self.lock.notify_all()
