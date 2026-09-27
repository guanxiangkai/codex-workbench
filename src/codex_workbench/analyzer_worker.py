"""Reset 分析器的受控调用边界。

后台快照线程通过此包装器调用分析器，单个账户失败不会中断整页采集。
"""
from __future__ import annotations

import threading
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .reset_analysis import ResetAnalyzer, unavailable_analysis


class ResetAnalysisWorker:
    def __init__(self, analyzer: ResetAnalyzer, *, cache_path: Path | None = None, interval: float = 1800):
        self.analyzer = analyzer
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.cache_path = cache_path
        self.interval = interval
        self._result = None
        self._next_attempt = 0.0
        if cache_path:
            try:
                value = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("scope") == "official_manual_reset":
                    age = time.time() - datetime.fromisoformat(value["observed_at"]).timestamp()
                    self._result = value
                    self.analyzer.history = value.get("history", [])[-64:]
                    retry_interval = 300 if value.get("error") else interval
                    self._next_attempt = time.monotonic() + max(0, min(retry_interval, retry_interval - age))
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def cached(self, account: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Return the shared public result without invoking inference."""
        with self._lock:
            return copy.deepcopy(self._result) if self._result is not None else None

    def force_refresh(self, account: Mapping[str, Any]) -> dict[str, Any]:
        """Explicitly refresh the shared manual-reset analysis."""
        with self._refresh_lock:
            try:
                result = self.analyzer.analyze_account(account)
                if not isinstance(result, dict):
                    result = unavailable_analysis({}, "invalid_analysis")
            except Exception:
                result = unavailable_analysis({})
            failed = bool(result.get("error"))
            if failed and self._result is not None and (not self._result.get("error") or self._result.get("last_success_at")):
                result = dict(self._result, stale=True, error=result["error"],
                              last_success_at=self._result.get("last_success_at") or self._result.get("observed_at"),
                              last_attempt_at=datetime.now(timezone.utc).isoformat())
            with self._lock:
                self._result = result
                self._next_attempt = time.monotonic() + (300 if failed else self.interval)
            self._save(result)
            return copy.deepcopy(result)

    def analyze_account(self, account: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibility entry; ordinary projections use cached() exclusively."""
        with self._lock:
            if self._result is not None and time.monotonic() < self._next_attempt:
                return copy.deepcopy(self._result)
        return self.force_refresh(account)

    def _save(self, result: dict[str, Any]) -> None:
        if self.cache_path:
            temporary = self.cache_path.with_suffix(".tmp")
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    os.chmod(temporary, 0o600)
                    json.dump(result, handle, ensure_ascii=False)
                temporary.replace(self.cache_path)
            except OSError:
                pass
